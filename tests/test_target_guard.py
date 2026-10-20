"""대상 확인으로 잘못된 서비스 조회를 막는지 그래프 전체에서 검증한다."""
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph

ROOT = Path(__file__).resolve().parent.parent


class RoutingStub(FastLLM):
    def route(self, query):
        # 대상 검증과 무관한 LLM 라우터의 변동을 분리한다.
        return {"route": "chat" if query in ("안녕", "payments-service") else "incident",
                "needs_analysis": False}, {"usd": 0.0}


class TargetGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fast = RoutingStub("test-fast", 0)
        self.mcp = AsyncMock()
        self.mcp.find_prompt.return_value = "ops"
        self.mcp.get_prompt.return_value = "장애 진단 절차"
        self.mcp.find_resource_template.return_value = (
            "ops", "runbook://incident-response/{incident_type}")

        async def monitor(name, arguments):
            target = arguments["system_name"]
            data = json.loads((ROOT / "data/monitoring.json").read_text())[target]
            return {"system_name": target, "status": data["status"],
                    "cpu_percent": data["cpu_base"], "alerts": data["alerts"]}

        async def runbook(uri, server):
            return (ROOT / "data/runbooks" / (uri.rsplit("/", 1)[-1] + ".md")).read_text()

        self.mcp.call_tool.side_effect = monitor
        self.mcp.read_resource.side_effect = runbook
        self.graph = build_graph(SupervisorDeps(
            self.fast, QualityLLM("test-quality", 0), self.mcp, None, "v2"))

    async def invoke(self, query, history=None):
        return await self.graph.ainvoke({"query": query, "history": history or [],
                                         "events": [], "cost_usd": 0.0})

    async def test_missing_and_unknown_never_query_mcp(self):
        for query, status in [
            ("billing-service 응답이 느려요", "unknown"),
            ("응답이 느려요. 원인 확인해주세요.", "missing"),
            ("payments-service와 billing-service 지연", "unknown"),
            ("my-auth-service 지연", "unknown"),
            ("auth-service-extra 지연", "missing"),
        ]:
            with self.subTest(query=query):
                result = await self.invoke(query)
                self.assertEqual(result["target_status"], status)
                self.assertIn("서비스", result["final"])
                self.assertEqual(result["tasks"], [])
                self.mcp.find_prompt.assert_not_awaited()
                self.mcp.call_tool.assert_not_awaited()

    async def test_each_registered_service_queries_only_requested_target(self):
        for target in json.loads((ROOT / "data/monitoring.json").read_text()):
            with self.subTest(target=target):
                self.mcp.call_tool.reset_mock()
                result = await self.invoke(f"{target} 지연 원인 진단")
                actual = [c.args[1]["system_name"] for c in self.mcp.call_tool.await_args_list]
                self.assertEqual(actual, [target])
                self.assertTrue(result["final"])

    async def test_multiple_targets_and_case_are_preserved(self):
        await self.invoke("PAYMENTS-SERVICE와 cache-service 지연 진단")
        actual = {c.args[1]["system_name"] for c in self.mcp.call_tool.await_args_list}
        self.assertEqual(actual, {"payments-service", "cache-service"})

    async def test_clarification_reply_resumes_original_symptom(self):
        for original in ["응답이 느려요. 원인 확인해주세요.", "billing-service 응답이 느려요"]:
            first = await self.invoke(original)
            result = await self.invoke("payments-service", [
                {"role": "user", "content": original},
                {"role": "assistant", "content": first["final"]},
            ])
            self.assertEqual(result["targets"], ["payments-service"])
            self.assertIn("느려요", result["query"])
            self.assertNotIn("billing-service", result["query"])

    async def test_greeting_stays_chat(self):
        result = await self.invoke("안녕")
        self.assertEqual(result["route"], "chat")
        self.assertNotIn("target_status", result)
        self.mcp.call_tool.assert_not_awaited()

    async def test_unreadable_registry_stops_instead_of_defaulting(self):
        with patch("agent.graph.load_known_systems", side_effect=OSError("unavailable")):
            result = await self.invoke("payments-service 지연")
        self.assertEqual(result["target_status"], "unavailable")
        self.mcp.call_tool.assert_not_awaited()

    async def test_stream_returns_clarification_as_final_answer(self):
        updates = [update async for update in self.graph.astream(
            {"query": "billing-service 응답이 느려요", "events": [], "cost_usd": 0.0},
            stream_mode="updates")]
        final = updates[-1]["check_target"]["final"]
        self.assertIn("billing-service", final)
        self.mcp.call_tool.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
