"""평가 점수의 분모·실패 처리·워밍업 제외와 과거 코드 격리를 검증한다."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from eval.metrics import score_turn, summarize
from eval.run_eval import load_dataset, main

ROOT = Path(__file__).resolve().parent.parent


def expected(targets=None, manuals=None):
    return {"query": "테스트", "expected_route": "incident",
            "expected_targets": targets or [], "expected_runbooks": manuals or []}


def actual(targets=None, manuals=None, **kwargs):
    return {"route": "incident", "monitoring_targets": targets or [],
            "runbook_types": manuals or [], "reply": "안내", "elapsed_seconds": 1.0, **kwargs}


def record(phase, exp, act):
    return {"phase": phase, "turns": [{**act, "score": score_turn(exp, act)}]}


class EvaluationMetricTests(unittest.TestCase):
    def test_target_accuracy_requires_exact_set_and_route(self):
        exp = expected(["payments-service"])
        self.assertTrue(score_turn(exp, actual(["payments-service"]))["target_correct"])
        self.assertFalse(score_turn(exp, actual(["payments-service", "auth-service"]))["target_correct"])
        self.assertFalse(score_turn(exp, actual(["payments-service"], route="chat"))["target_correct"])

    def test_no_lookup_without_clarification_is_not_a_success(self):
        exp = {**expected(), "required_reply_tokens": ["서비스", "알려"]}
        self.assertFalse(score_turn(exp, actual())["target_correct"])
        self.assertTrue(score_turn(exp, actual(reply="서비스명을 알려주세요"))["target_correct"])

    def test_manual_recall_deduplicates_and_records_unwanted_types(self):
        exp = expected(manuals=["cpu-spike", "memory-leak"])
        score = score_turn(exp, actual(manuals=["cpu-spike", "cpu-spike", "db-down"]))
        self.assertEqual((score["manual_hits"], score["manual_required"]), (1, 2))
        self.assertEqual(score["unexpected_manuals"], ["db-down"])
        self.assertFalse(score["manual_exact"])

    def test_no_expected_manual_has_no_recall_denominator(self):
        result = summarize([record("measured", expected(), actual())])
        self.assertIsNone(result["manual_recall"])

    def test_failed_runs_still_count_against_accuracy(self):
        exp = expected(["payments-service"], ["cpu-spike"])
        good = actual(["payments-service"], ["cpu-spike"])
        bad = actual(["payments-service"], ["cpu-spike"], error="timeout", elapsed_seconds=300)
        result = summarize([record("measured", exp, good), record("measured", exp, bad)])
        self.assertEqual(result["target_accuracy"], 0.5)
        self.assertEqual(result["manual_recall"], 0.5)
        self.assertEqual(result["latency_samples"], 1)
        self.assertEqual(result["latency_median_seconds"], 1)
        self.assertEqual(result["error_turns"], 1)

    def test_warmups_are_excluded_and_median_uses_repetitions(self):
        exp = expected()
        rows = [record("warmup", exp, actual(elapsed_seconds=99))]
        rows += [record("measured", exp, actual(elapsed_seconds=v)) for v in [1, 9, 3]]
        result = summarize(rows)
        self.assertEqual(result["turns"], 3)
        self.assertEqual(result["latency_median_seconds"], 3)

    def test_all_failed_has_no_fabricated_zero_latency(self):
        result = summarize([record("measured", expected(), actual(error="failed", elapsed_seconds=None))])
        self.assertIsNone(result["latency_median_seconds"])
        self.assertEqual(result["target_accuracy"], 0)

    def test_unknown_case_filter_is_rejected(self):
        with self.assertRaises(ValueError):
            load_dataset(ROOT / "eval/cases.json", ["does-not-exist"])


class EvaluationRunnerTests(unittest.TestCase):
    def test_interruption_is_saved_without_claiming_completed_measurements(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "interrupted"
            with patch("eval.run_eval.Worker", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    main(["--case", "unknown_service", "--output-dir", str(output)])
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["manifest"]["status"], "interrupted")
            self.assertEqual(summary["summary"]["after"]["turns"], 0)
            self.assertIsNone(summary["summary"]["after"]["latency_median_seconds"])
            self.assertIn("interrupted", (output / "report.md").read_text())

    def test_current_and_real_git_baseline_are_isolated_and_outputs_are_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "results"
            command = [sys.executable, "-m", "eval.run_eval", "--backend", "fake",
                       "--baseline-ref", "741cd7e", "--case", "payments_cache",
                       "--case", "unknown_service", "--case", "clarify_followup",
                       "--repeats", "2", "--warmups", "1", "--output-dir", str(output)]
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["manifest"]["status"], "complete")
            self.assertTrue(summary["manifest"]["variants"]["before"]["revision"].startswith("741cd7e"))
            self.assertNotEqual(summary["manifest"]["variants"]["before"]["fingerprint"]["sha256"],
                                summary["manifest"]["variants"]["after"]["fingerprint"]["sha256"])
            self.assertEqual(summary["summary"]["after"]["turns"], 8)
            self.assertEqual(summary["summary"]["after"]["target_accuracy"], 1)
            self.assertEqual(summary["summary"]["after"]["manual_recall"], 1)
            self.assertEqual(summary["summary"]["after"]["error_turns"], 0)
            self.assertLess(summary["summary"]["before"]["manual_recall"], 1)
            rows = [json.loads(line) for line in (output / "raw_results.jsonl").read_text().splitlines()]
            measured = [r for r in rows if r["phase"] == "measured"]
            self.assertEqual([r["variant"] for r in measured[:2]], ["before", "after"])
            self.assertEqual([r["variant"] for r in measured[6:8]], ["after", "before"])
            self.assertTrue((output / "results.csv").is_file())
            self.assertIn("모의 모델", (output / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
