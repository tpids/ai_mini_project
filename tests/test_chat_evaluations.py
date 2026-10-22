"""방금 공개한 답변과 그때의 근거를 평가하며 진단을 재실행하지 않는지 검증한다."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ['LANGCHAIN_TRACING_V2'] = 'false'
os.environ['LANGSMITH_TRACING'] = 'false'
from fastapi.testclient import TestClient
import server
from agent.chat_evaluations import ChatEvaluationStore
from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.session_store import SqliteSessionStore
from eval.worker import FixtureMCP

ROOT = Path(__file__).resolve().parent.parent


class SnapshotTests(unittest.TestCase):
    def test_saved_answer_is_immutable_and_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ChatEvaluationStore(temp)
            state = {'query': '질문', 'final': '현재 CPU 95%', 'collected': [{'source': 'monitoring', 'data': {'cpu_percent': 95}}]}
            key = store.capture('질문', state, {})
            snapshot = store.load(key)
            store.save_judgment(snapshot, {'status': 'error', 'error': 'timeout'})
            self.assertEqual(store.load(key), snapshot)
            payload = json.loads((Path(temp) / f'{key}.json').read_text())
            payload['reply'] = '변경된 답변'
            (Path(temp) / f'{key}.json').write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                store.load(key)
            with self.assertRaises(ValueError):
                store.load('../session')

    def test_normal_chat_without_monitoring_does_not_get_a_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ChatEvaluationStore(temp)
            self.assertIsNone(store.capture('안녕', {'final': '안녕하세요'}, {}))
            self.assertEqual(list(Path(temp).iterdir()), [])


class ChatEvaluationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ChatEvaluationStore(self.temp.name)
        self.mcp = FixtureMCP(ROOT)
        self.mcp.monitoring = json.loads((ROOT / 'eval/cases.json').read_text())['monitoring']
        self.sessions = SqliteSessionStore(':memory:')
        self.addCleanup(self.sessions.close)
        deps = SupervisorDeps(FastLLM('test', 0), QualityLLM('test', 0), self.mcp, self.sessions, 'v2')
        for key, value in {'deps': deps, 'graph': build_graph(deps), 'chat_evaluations': self.store,
                           'judge_lock': asyncio.Lock(), 'llm_backend': 'fake', 'instance_id': 0}.items():
            p = patch.object(server.app.state, key, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        # lifespan의 실제 MCP/Ollama 대신 위 의존성을 사용한다.
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def test_chat_button_scores_the_exact_reply_and_does_not_rediagnose(self):
        response = self.client.post('/chat', json={'message': 'payments-service CPU 급증 진단해줘', 'thread_id': 'live'})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        key = body['metadata']['evaluation_id']
        snapshot = self.store.load(key)
        self.assertEqual(snapshot['reply'], body['reply'])
        self.assertEqual(snapshot['collected'][0]['data']['cpu_percent'], 91.0)
        calls = list(self.mcp.monitoring_targets)
        judge = Mock()
        judge.evaluate.return_value = {'status': 'ok', 'criteria': {'numbers': {'score': 2}}, 'model': 'test'}
        with patch('server._build_judge', return_value=judge):
            result = self.client.post(f'/evaluations/{key}/judge')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['snapshot_sha256'], snapshot['content_sha256'])
        self.assertEqual(judge.evaluate.call_args.args, (snapshot['query'], snapshot['collected'], body['reply']))
        self.assertEqual(self.mcp.monitoring_targets, calls)
        self.assertEqual(self.store.load(key), snapshot)

    def test_stream_exposes_an_id_for_its_own_final_answer_and_full_evidence(self):
        response = self.client.get('/chat/stream', params={'message': 'web-01 CPU 급증, auth-service 커넥션 풀도 같이 확인해줘', 'thread_id': 'sse'})
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith('data: ') and line != 'data: [DONE]']
        final = next(e for e in events if e['type'] == 'FinalAnswer')
        snapshot = self.store.load(final['evaluation_id'])
        self.assertEqual(snapshot['reply'], final['reply'])
        self.assertEqual({c['data']['system_name'] for c in snapshot['collected'] if c['source'] == 'monitoring'}, {'web-01', 'auth-service'})
        self.assertTrue(any(c['source'] == 'runbook' for c in snapshot['collected']))
        self.assertTrue(response.text.rstrip().endswith('data: [DONE]'))

    def test_unknown_target_has_no_evaluation_button_and_invalid_ids_are_rejected(self):
        body = self.client.post('/chat', json={'message': 'billing-service 지연 진단해줘'}).json()
        self.assertIsNone(body['metadata']['evaluation_id'])
        self.assertEqual(self.client.post('/evaluations/bad/judge').status_code, 400)
        self.assertEqual(self.client.post('/evaluations/' + '0'*32 + '/judge').status_code, 404)

    def test_judge_failure_is_not_shown_as_a_valid_score(self):
        body = self.client.post('/chat', json={'message': 'payments-service CPU 급증 진단해줘'}).json()
        key = body['metadata']['evaluation_id']
        judge = Mock()
        judge.evaluate.return_value = {'status': 'error', 'error': 'timeout'}
        with patch('server._build_judge', return_value=judge):
            result = self.client.post(f'/evaluations/{key}/judge').json()
        self.assertEqual(result['judgment']['status'], 'error')
        self.assertNotIn('criteria', result['judgment'])

class JudgeProxyTests(unittest.TestCase):
    def test_frontend_proxy_passes_the_selected_answer_id_and_result(self):
        from unittest.mock import AsyncMock
        import web_demo_server
        response = Mock()
        response.status_code = 200
        response.json.return_value = {'snapshot_id': 'a'*32, 'judgment': {'status': 'ok'}}
        upstream = AsyncMock()
        upstream.post.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = upstream
        with patch('web_demo_server.httpx.AsyncClient', return_value=context):
            client = TestClient(web_demo_server.app)
            result = client.post('/evaluations/' + 'a'*32 + '/judge')
            client.close()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), response.json.return_value)
        upstream.post.assert_awaited_once_with('http://127.0.0.1:8100/evaluations/' + 'a'*32 + '/judge')
