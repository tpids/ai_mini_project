"""LLM 평가의 오류·비적용·원문 인용·집계와 답변 불변성을 검증한다."""
import json
import unittest
from unittest.mock import Mock, patch

from eval.judge import CRITERIA, OllamaJudge, applicability, parse_verdict, prompt
from eval.judge_results import summarize_judgments


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.reply = "CPU 95%. 지속 여부는 확인하지 않았습니다."
        self.collected = [{"source": "monitoring", "data": {"system_name": "payments-service", "cpu_percent": 95}},
                          {"source": "runbook", "system_name": "payments-service", "incident_type": "cpu-spike",
                           "data": "1. CPU 90%를 넘고 지속되면 확장한다."}]
        self.verdict = {key: {"score": 2, "reason": "원문과 일치", "line_id": 1} for key in CRITERIA}

    def test_scores_require_all_criteria_and_exact_quotes(self):
        self.assertEqual(parse_verdict(json.dumps(self.verdict), self.reply, list(CRITERIA))["numbers"]["quote"], self.reply)
        self.verdict["numbers"]["line_id"] = 999
        with self.assertRaises(ValueError):
            parse_verdict(json.dumps(self.verdict), self.reply, list(CRITERIA))
        self.verdict.pop("numbers")
        with self.assertRaises(ValueError):
            parse_verdict(json.dumps(self.verdict), self.reply, list(CRITERIA))

    def test_boolean_and_out_of_range_scores_are_rejected(self):
        for value in [True, 3, "2", None]:
            self.verdict["numbers"]["score"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_verdict(json.dumps(self.verdict), self.reply, list(CRITERIA))

    def test_missing_manual_is_not_fabricated_as_a_zero_condition_score(self):
        applicable = applicability(self.collected[:1])
        self.assertNotIn("conditions", applicable)
        self.verdict["conditions"]["score"] = None
        result = parse_verdict(json.dumps(self.verdict), self.reply, applicable)
        self.assertIsNone(result["conditions"]["score"])

    def test_prompt_contains_raw_facts_and_no_before_after_label(self):
        system, user, schema = prompt("CPU 진단", self.collected, self.reply)
        self.assertIn("수집된 사실", system)
        self.assertIn("95", user)
        self.assertNotIn('"variant"', user)
        self.assertEqual(set(schema["required"]), set(CRITERIA))

    def test_schema_forbids_null_evidence_lines_for_applicable_criteria(self):
        _, _, schema = prompt("CPU 진단", self.collected, self.reply)
        for key in CRITERIA:
            fields = schema["properties"][key]["properties"]
            self.assertEqual(fields["line_id"]["type"], "integer")
            self.assertNotIn(None, fields["line_id"]["enum"])
        _, _, schema = prompt("CPU 진단", self.collected[:1], self.reply)
        self.assertEqual(schema["properties"]["conditions"]["properties"]["score"]["type"], "null")

    def test_call_is_a_real_llm_request_but_never_mutates_the_answer(self):
        response = Mock()
        response.json.return_value = {"message": {"content": json.dumps(self.verdict)}}
        with patch("eval.judge.httpx.post", return_value=response) as post:
            result = OllamaJudge().evaluate("CPU 진단", self.collected, self.reply)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(post.call_args.kwargs["json"]["think"])
        self.assertEqual(self.reply, "CPU 95%. 지속 여부는 확인하지 않았습니다.")

    def test_model_failure_is_an_error_not_a_perfect_or_zero_score(self):
        with patch("eval.judge.httpx.post", side_effect=TimeoutError("timeout")):
            result = OllamaJudge().evaluate("CPU 진단", self.collected, self.reply)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("criteria", result)

    def test_aggregation_keeps_errors_skips_and_applicable_denominators(self):
        self.verdict["conditions"]["score"] = None
        rows = [{"variant": "after", "judgment": j} for j in [
            {"status": "ok", "criteria": self.verdict}, {"status": "error"}, {"status": "skipped"}]]
        result = summarize_judgments(rows)["after"]
        self.assertEqual((result["valid"], result["errors"], result["skipped"]), (1, 1, 1))
        self.assertEqual(result["criteria"]["numbers"], {"mean": 2, "n": 1})
        self.assertEqual(result["criteria"]["conditions"], {"mean": None, "n": 0})
