"""RAG 추가 시 대상·근거·다중 매뉴얼 보존 및 벡터 캐시 무효화를 검증한다."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.reporting import report_is_grounded
from agent.retrieval import RunbookRetriever, normalized
from eval.worker import FixtureMCP

ROOT = Path(__file__).resolve().parent.parent


class EmbeddingStub:
    """테스트 전용 벡터. 실제 의미 검색 성능 점수에 사용하지 않는다."""
    identity = "test-vectors@1"

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return [[1.0, 0.0] if 'CPU' in t else [0.0, 1.0] for t in texts]


class VectorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = self.root / "data/runbooks"
        self.docs.mkdir(parents=True)
        (self.docs / "cpu-spike.md").write_text("# CPU\n1. CPU 90%를 넘고 지속되면 확장한다.\n")
        (self.docs / "memory-leak.md").write_text("# 메모리\n1. OOM이면 임시 재시작한다.\n")
        self.embeddings = EmbeddingStub()
        self.retriever = RunbookRetriever(self.root, self.root / "vectors.db", self.embeddings)

    def test_vectors_are_persisted_and_unchanged_documents_are_not_embedded_again(self):
        first = self.retriever.search("CPU")
        self.assertEqual(first["hits"][0]["incident_type"], "cpu-spike")
        self.assertEqual(first["hits"][0]["score"], 1)
        self.retriever.search("메모리")
        self.assertEqual([len(c) for c in self.embeddings.calls], [2, 1, 1])
        self.assertTrue((self.root / "vectors.db").is_file())

    def test_document_change_deletion_and_model_change_refresh_vectors(self):
        self.retriever.search("CPU")
        (self.docs / "cpu-spike.md").write_text("# CPU\n1. 변경된 조건은 95% 지속이다.\n")
        changed = self.retriever.search("CPU")
        self.assertEqual([len(c) for c in self.embeddings.calls], [2, 1, 2, 1])
        self.assertEqual(changed["hits"][0]["sha256"], self.retriever.documents()[0]["sha256"])
        (self.docs / "memory-leak.md").unlink()
        self.assertEqual(len(self.retriever.search("CPU")["hits"]), 1)
        self.embeddings.identity = "test-vectors@2"
        self.assertEqual(self.retriever.search("CPU")["model"], "test-vectors@2")

    def test_invalid_vectors_are_rejected(self):
        for value in [[], [0, 0], [float("nan")], [True], [float("inf")]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalized(value)

    def test_search_never_forces_a_manual_on_a_healthy_service(self):
        self.retriever.search = Mock(side_effect=AssertionError("검색하면 안 됨"))
        result = self.retriever.select({"status": "healthy", "alerts": [], "cpu_percent": 30}, [])
        self.assertEqual(result["incident_types"], [])
        self.retriever.search.assert_not_called()

    def test_bad_ranking_cannot_drop_existing_multiple_rule_types(self):
        self.retriever.search = Mock(return_value={"hits": [{"incident_type": "db-down", "score": 0.99}]})
        result = self.retriever.select({"status": "degraded", "alerts": ["CPU high", "heap full"]},
                                       ["cpu-spike", "memory-leak"])
        self.assertEqual(result["incident_types"], ["cpu-spike", "memory-leak"])
        self.assertNotIn("db-down", result["incident_types"])

    def test_semantic_match_and_ambiguous_result_have_different_outcomes(self):
        self.retriever.search = Mock(return_value={"hits": [{"incident_type": "memory-leak", "score": 0.8},
                                                             {"incident_type": "cpu-spike", "score": 0.4}]})
        data = {"status": "degraded", "alerts": ["회수되지 않는 객체가 쌓임"]}
        self.assertEqual(self.retriever.select(data, [])["incident_types"], ["memory-leak"])
        self.retriever.search.return_value["hits"][1]["score"] = 0.79
        self.assertEqual(self.retriever.select(data, [])["incident_types"], [])


class RagPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def run_pipeline(self, retriever, case_id="payments_cache"):
        dataset = json.loads((ROOT / "eval/cases.json").read_text())
        case = next(c for c in dataset["cases"] if c["id"] == case_id)
        mcp = FixtureMCP(ROOT)
        mcp.monitoring = dataset["monitoring"]
        deps = SupervisorDeps(FastLLM("test", 0), QualityLLM("test", 0), mcp, None, "v2", retriever)
        result = await build_graph(deps).ainvoke({"query": case["turns"][0]["query"], "events": [], "history": [], "cost_usd": 0})
        return result, mcp

    async def test_embedding_failure_preserves_old_behavior_and_is_visible(self):
        retriever = Mock()
        retriever.select.side_effect = TimeoutError("offline")
        result, mcp = await self.run_pipeline(retriever)
        self.assertEqual(set(mcp.runbook_types), {"cpu-spike", "memory-leak"})
        self.assertEqual(retriever.select.call_count, 2)  # 재계획 후 검색 중복 없음
        self.assertTrue(any(e.get("method") == "rule_fallback" for e in result["events"]))
        self.assertTrue(report_is_grounded(result["final"], result["collected"], result["report_step_ids"]))
        self.assertIn("CPU가 90%를 넘고 지속되면", result["final"])

    async def test_successful_vector_search_preserves_both_full_source_manuals(self):
        with tempfile.TemporaryDirectory() as temp:
            retriever = RunbookRetriever(ROOT, Path(temp) / "vectors.db", EmbeddingStub())
            result, mcp = await self.run_pipeline(retriever)
        self.assertEqual(set(mcp.runbook_types), {"cpu-spike", "memory-leak"})
        self.assertTrue(any(e.get("method") == "rag_with_rule_coverage" for e in result["events"]))
        for item in result["collected"]:
            if item["source"] == "runbook":
                self.assertEqual(item["data"], (ROOT / "data/runbooks" / (item["incident_type"] + ".md")).read_text())
        self.assertTrue(report_is_grounded(result["final"], result["collected"], result["report_step_ids"]))

    async def test_missing_and_unknown_targets_never_reach_retrieval(self):
        for case in ["unknown_service", "missing_target"]:
            retriever = Mock()
            result, mcp = await self.run_pipeline(retriever, case)
            retriever.select.assert_not_called()
            self.assertEqual(mcp.monitoring_targets, [])
            self.assertIn("진단할 서비스 확인이 필요합니다", result["final"])
