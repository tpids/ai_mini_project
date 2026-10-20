"""관측값·조건·미확인 사항을 구분하고, 잘못된 초안의 공개를 막는 회귀 검증."""
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM
from agent.graph import build_graph, validate_node
from agent.ollama_llm import OllamaQualityLLM
from agent.openai_llm import OpenAIQualityLLM
from agent.reporting import (
    decode_selection, default_step_ids, render_report, report_is_grounded, runbook_steps,
)

ROOT = Path(__file__).resolve().parent.parent


def cpu_facts(cpu=92.3):
    return [
        {"source": "monitoring", "data": {
            "system_name": "payments-service", "status": "degraded",
            "cpu_percent": cpu, "memory_percent": 55.6,
            "alerts": ["CPU 91% (threshold 80%)", "payments-service-3 instance responding slow"]}},
        {"source": "runbook", "data": (ROOT / "data/runbooks/cpu-spike.md").read_text()},
        {"source": "analysis", "data": "특이 패턴 없음"},
    ]


class EvidenceReportTests(unittest.TestCase):
    def test_exact_values_and_alerts_are_separate_from_manual_thresholds(self):
        report = render_report(cpu_facts(), ["R1-S3"])
        facts, rest = report.split("2) 아직 확인하지 못한 내용")
        self.assertIn("CPU 92.3%", facts)
        self.assertIn("메모리 55.6%", facts)
        self.assertIn("CPU 91% (threshold 80%)", facts)
        self.assertIn("CPU가 90%를 넘고 지속되면", rest)
        self.assertIn("지속 여부는 검증하지 않았습니다", rest)

    def test_below_threshold_still_keeps_action_conditional(self):
        report = render_report(cpu_facts(88.7), ["R1-S3"])
        self.assertIn("CPU 88.7%", report)
        self.assertIn("CPU가 90%를 넘고 지속되면", report)
        self.assertIn("조건 충족 여부를 추가 확인", report)

    def test_slow_instance_name_never_becomes_a_new_instance(self):
        report = render_report(cpu_facts(), ["R1-S3"])
        actions = report.split("3) 매뉴얼에 따른 점검·조치 권고")[1]
        self.assertNotIn("payments-service-3", actions)
        self.assertIn("payments-service-3", report.split("2)")[0])

    def test_model_pattern_summary_is_not_log_or_region_evidence(self):
        report = render_report(cpu_facts(), ["R1-S2"])
        self.assertNotIn("특이 패턴 없음", report)
        self.assertIn("최근 로그·배포 이력·리전별 분포는 수집하지 않았습니다", report)

    def test_every_runbook_step_is_preserved_verbatim(self):
        for path in (ROOT / "data/runbooks").glob("*.md"):
            with self.subTest(runbook=path.name):
                collected = [{"source": "runbook", "data": path.read_text()}]
                for step_id, step in runbook_steps(collected).items():
                    self.assertIn(step["text"], render_report(collected, [step_id]))

    def test_unavailable_metrics_are_not_fabricated(self):
        collected = [{"source": "monitoring", "data": {"system_name": "web-01"}}]
        report = render_report(collected, [])
        self.assertNotIn("0%", report)
        self.assertIn("상태=미수집", report)
        self.assertIn("매뉴얼 항목이 수집되지 않았습니다", report)

    def test_condition_on_continuation_line_is_preserved(self):
        facts = [{"source": "runbook", "data":
                  "# CPU 대응\n\n1. 인스턴스 추가를 검토한다.\n   단, CPU 90% 초과가 지속될 때만 적용한다.\n\n2. 로그를 확인한다."}]
        report = render_report(facts, ["R1-S1"])
        self.assertIn("단, CPU 90% 초과가 지속될 때만 적용한다.", report)

    def test_invalid_selection_is_rejected(self):
        for content in ['not json', 'null', '{}', '{"step_ids":[]}',
                        '{"step_ids":["R1-S99"]}', '{"step_ids":[null]}',
                        '{"step_ids":["R1-S1","R1-S1"]}']:
            with self.subTest(content=content), self.assertRaises(ValueError):
                decode_selection(content, cpu_facts())

    def test_added_claims_and_changed_conditions_fail_validation(self):
        facts = cpu_facts()
        selected = ["R1-S3"]
        good = render_report(facts, selected)
        for bad in [good.replace("92.3%", "99.9%"),
                    good.replace("90%를 넘고 지속되면", "90%를 넘었으므로"),
                    good + "\n로그를 확인했으며 이상이 없습니다."]:
            with self.subTest(draft=bad):
                self.assertFalse(report_is_grounded(bad, facts, selected))


class ReportPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_model_backends_validate_selection_and_fall_back(self):
        for cls in [OllamaQualityLLM, OpenAIQualityLLM]:
            for content, mode in [('{"step_ids":["R1-S3"]}', "source_selected"),
                                  ('{"step_ids":["invented"]}', "source_fallback")]:
                with self.subTest(backend=cls.__name__, content=content):
                    model = cls("test-model", 0)
                    model._chat = Mock(return_value=(content, {"usd": 0.0}))
                    callback = Mock()
                    report, usage = await model.write("CPU 급증", cpu_facts(), 1, None, callback)
                    self.assertEqual(usage["report_mode"], mode)
                    self.assertTrue(report_is_grounded(report, cpu_facts(), usage["report_step_ids"]))
                    callback.assert_not_called()

    async def test_missing_manual_does_not_call_model(self):
        model = OllamaQualityLLM("test-model", 0)
        model._chat = Mock(side_effect=AssertionError("must not call"))
        report, usage = await model.write("CPU 급증", cpu_facts()[:1], 1, None)
        self.assertIn("매뉴얼 항목이 수집되지 않았습니다", report)
        self.assertEqual(usage["report_mode"], "no_runbook")

    async def test_maximum_attempts_replace_bad_draft_instead_of_approving_it(self):
        state = {"draft": "1) 없는 수치 99.9%로 즉시 재시작 완료", "collected": cpu_facts(),
                 "report_step_ids": ["R1-S3"], "events": [], "validation_attempts": 2}
        update = await validate_node(state, None)
        self.assertIsNone(update["validation_feedback"])
        self.assertNotIn("99.9%", update["draft"])
        self.assertEqual(update["report_mode"], "source_fallback")
        self.assertEqual(update["events"][-1]["verdict"], "원문 보고서로 대체")

    async def test_bad_drafts_are_never_streamed_to_user(self):
        quality = AsyncMock()
        quality.write.return_value = ("1) 로그 확인 완료, 새 인스턴스 생성 완료",
                                      {"usd": 0, "report_step_ids": ["R1-S3"]})
        mcp = AsyncMock()
        mcp.find_prompt.return_value = "ops"
        mcp.get_prompt.return_value = "표준 진단 절차"
        mcp.call_tool.return_value = cpu_facts()[0]["data"]
        mcp.find_resource_template.return_value = ("ops", "runbook://{incident_type}")
        mcp.read_resource.return_value = cpu_facts()[1]["data"]
        graph = build_graph(SupervisorDeps(FastLLM("test-fast", 0), quality, mcp, None, "v2"))
        final = None
        async for mode, payload in graph.astream(
                {"query": "payments-service CPU 급증 진단", "history": [], "events": [], "cost_usd": 0},
                stream_mode=["updates", "custom"]):
            if mode == "custom":
                self.assertNotEqual(payload.get("type"), "AnswerToken")
            elif "finalize" in payload:
                final = payload["finalize"]["final"]
        self.assertEqual(quality.write.await_count, 3)
        self.assertNotIn("새 인스턴스 생성 완료", final)
        self.assertNotIn("로그 확인 완료", final)
        self.assertIn("CPU가 90%를 넘고 지속되면", final)


if __name__ == "__main__":
    unittest.main()
