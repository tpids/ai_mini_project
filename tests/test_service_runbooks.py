"""복수 서비스·장애 유형에서 매뉴얼과 대상 연결이 빠지지 않는지 검증한다."""
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph, replan_check_node
from agent.incident_rules import classify_incidents
from agent.ollama_llm import OllamaQualityLLM
from agent.reporting import decode_selection, default_step_ids, report_is_grounded, selected_report

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "tests/fixtures/service_runbooks.json").read_text())


class RoutingStub(FastLLM):
    def route(self, query):
        return {"route": "incident", "needs_analysis": False}, {"usd": 0}


async def run_case(case, quality=None):
    monitoring = {d["system_name"]: d for d in case["monitoring"]}
    mcp = AsyncMock()
    mcp.find_prompt.return_value = "ops"
    mcp.get_prompt.return_value = "진단 절차"
    mcp.find_resource_template.return_value = ("ops", "runbook://incident-response/{incident_type}")

    async def monitor(name, arguments):
        return monitoring[arguments["system_name"]]

    async def read(uri, server):
        return (ROOT / "data/runbooks" / (uri.rsplit("/", 1)[-1] + ".md")).read_text()

    mcp.call_tool.side_effect = monitor
    mcp.read_resource.side_effect = read
    graph = build_graph(SupervisorDeps(RoutingStub("test-router", 0),
                                      quality or QualityLLM("test-writer", 0), mcp, None, "v2"))
    result = await graph.ainvoke({"query": case["query"], "events": [], "cost_usd": 0, "history": []})
    return result, mcp


def scope_pairs(result):
    return {(c["system_name"], c["incident_type"]) for c in result["collected"] if c["source"] == "runbook"}


class ServiceRunbookTests(unittest.IsolatedAsyncioTestCase):
    async def test_payments_cpu_and_cache_memory_both_get_recommendations(self):
        result, mcp = await run_case(CASES[0])
        self.assertEqual(scope_pairs(result), {("payments-service", "cpu-spike"),
                                               ("cache-service", "memory-leak")})
        actions = result["final"].split("3) 매뉴얼에 따른 점검·조치 권고")[1]
        self.assertIn("대상 서비스: payments-service", actions)
        self.assertIn("대상 서비스: cache-service", actions)
        self.assertIn("CPU 급증 대응 절차", actions)
        self.assertIn("메모리 누수(Memory Leak) 대응 절차", actions)
        self.assertEqual(mcp.read_resource.await_count, 2)
        self.assertTrue(report_is_grounded(result["final"], result["collected"], result["report_step_ids"]))

    async def test_healthy_web_gets_no_cpu_manual_and_auth_is_labeled(self):
        result, mcp = await run_case(CASES[1])
        self.assertEqual(scope_pairs(result), {("auth-service", "high-latency")})
        actions = result["final"].split("3) 매뉴얼에 따른 점검·조치 권고")[1]
        self.assertIn("대상 서비스: auth-service", actions)
        self.assertNotIn("대상 서비스: web-01", actions)
        self.assertNotIn("CPU 급증 대응 절차", actions)
        self.assertIn("과거 급증이나 일시적 장애 여부는 시계열 확인이 필요합니다", result["final"])
        self.assertEqual(mcp.read_resource.await_count, 1)

    async def test_same_manual_is_fetched_once_but_linked_to_both_services(self):
        case = {"query": "payments-service와 cache-service CPU 급증", "monitoring": [
            {"system_name": name, "status": "degraded", "cpu_percent": 95, "alerts": ["CPU high"]}
            for name in ("payments-service", "cache-service")]}
        result, mcp = await run_case(case)
        self.assertEqual(scope_pairs(result), {("payments-service", "cpu-spike"),
                                               ("cache-service", "cpu-spike")})
        self.assertEqual(mcp.read_resource.await_count, 1)
        self.assertIn("대상 서비스: cache-service", result["final"])

    async def test_one_service_with_two_symptoms_gets_both_manuals(self):
        case = {"query": "payments-service CPU와 메모리 급증", "monitoring": [
            {"system_name": "payments-service", "status": "degraded",
             "cpu_percent": 95, "memory_percent": 94, "alerts": []}]}
        result, _ = await run_case(case)
        self.assertEqual(scope_pairs(result), {("payments-service", "cpu-spike"),
                                               ("payments-service", "memory-leak")})

    async def test_model_omitting_second_service_is_replaced_with_balanced_sources(self):
        quality = OllamaQualityLLM("test-model", 0)
        quality._chat = Mock(return_value=('{"step_ids":["R1-S1"]}', {"usd": 0}))
        result, _ = await run_case(CASES[0], quality)
        self.assertEqual(result["report_mode"], "source_fallback")
        self.assertIn("대상 서비스: payments-service", result["final"])
        self.assertIn("대상 서비스: cache-service", result["final"])

    async def test_no_signal_does_not_fall_back_to_db_down(self):
        case = {"query": "web-01 CPU 급증 진단", "monitoring": [CASES[1]["monitoring"][0]]}
        result, mcp = await run_case(case)
        self.assertEqual(scope_pairs(result), set())
        mcp.read_resource.assert_not_awaited()
        self.assertIn("현재 관측에서는 적용할 장애 매뉴얼이 없습니다", result["final"])

    async def test_unclassified_degradation_requests_more_evidence(self):
        case = {"query": "payments-service 장애 진단", "monitoring": [
            {"system_name": "payments-service", "status": "degraded", "alerts": ["unknown issue"]}]}
        result, mcp = await run_case(case)
        mcp.read_resource.assert_not_awaited()
        self.assertIn("매뉴얼 유형을 판별할 수 없습니다", result["final"])

    async def test_replanning_is_idempotent_once_all_scopes_are_collected(self):
        result, _ = await run_case(CASES[0])
        update = await replan_check_node(result, None)
        self.assertTrue(update["events"][-1]["is_sufficient"])
        self.assertNotIn("tasks", update)


class PerManualSelectionTests(unittest.TestCase):
    def setUp(self):
        self.collected = []
        for system, kind in [("payments-service", "cpu-spike"), ("cache-service", "memory-leak")]:
            self.collected.append({"source": "runbook", "system_name": system, "incident_type": kind,
                                   "data": (ROOT / "data/runbooks" / f"{kind}.md").read_text()})

    def test_selection_must_cover_each_manual(self):
        with self.assertRaises(ValueError):
            decode_selection('{"step_ids":["R1-S1","R1-S3"]}', self.collected)
        self.assertEqual(decode_selection('{"step_ids":["R1-S3","R2-S1"]}', self.collected),
                         ["R1-S3", "R2-S1"])

    def test_global_three_item_cap_does_not_drop_second_service(self):
        selected = default_step_ids(self.collected)
        self.assertEqual(len(selected), 6)
        report, usage = selected_report("bad json", self.collected, {"usd": 0})
        self.assertTrue(report_is_grounded(report, self.collected, usage["report_step_ids"]))
        self.assertIn("대상 서비스: cache-service", report)

    def test_more_than_three_items_from_one_manual_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_selection('{"step_ids":["R1-S1","R1-S2","R1-S3","R1-S4","R2-S1"]}', self.collected)

    def test_healthy_label_does_not_hide_a_high_metric(self):
        self.assertEqual(classify_incidents({"status": "healthy", "cpu_percent": 95, "alerts": []}),
                         ["cpu-spike"])


if __name__ == "__main__":
    unittest.main()
