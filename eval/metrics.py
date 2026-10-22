"""실행 결과와 사람이 지정한 정답을 비교한다. 에이전트의 분류 규칙은 사용하지 않는다."""
from statistics import median


def score_turn(expected: dict, actual: dict) -> dict:
    failed = bool(actual.get("error"))
    expected_targets = set(expected["expected_targets"])
    targets = set(actual.get("monitoring_targets", []))
    required = expected.get("required_reply_tokens", [])
    target_correct = (
        not failed and targets == expected_targets
        and actual.get("route") == expected["expected_route"]
        and all(token in actual.get("reply", "") for token in required)
    )
    wanted = set(expected["expected_runbooks"])
    retrieved = set(actual.get("runbook_types", [])) if not failed else set()
    return {
        "target_correct": target_correct,
        "manual_hits": len(wanted & retrieved),
        "manual_required": len(wanted),
        "unexpected_manuals": sorted(retrieved - wanted),
        "manual_exact": not failed and retrieved == wanted,
        "error": failed,
    }


def summarize(records: list[dict]) -> dict:
    measured = [r for r in records if r["phase"] == "measured"]
    turns = [t for r in measured for t in r["turns"]]
    denominator = len(turns)
    target_hits = sum(t["score"]["target_correct"] for t in turns)
    manual_hits = sum(t["score"]["manual_hits"] for t in turns)
    manual_total = sum(t["score"]["manual_required"] for t in turns)
    latencies = [t["elapsed_seconds"] for t in turns
                 if not t["score"]["error"] and t.get("elapsed_seconds") is not None]
    retrieval_events = [e for t in turns for e in t.get("events", []) if e.get("type") == "RunbookRetrieved"]
    return {
        "rag_searches": sum(e.get("method") in {"rag_with_rule_coverage", "rag_semantic", "rag_abstained"} for e in retrieval_events),
        "rag_fallbacks": sum(e.get("method") == "rule_fallback" for e in retrieval_events),
        "rag_semantic_matches": sum(e.get("method") == "rag_semantic" for e in retrieval_events),
        "case_runs": len(measured), "turns": denominator,
        "target_correct": target_hits,
        "target_accuracy": target_hits / denominator if denominator else None,
        "manual_hits": manual_hits, "manual_required": manual_total,
        "manual_recall": manual_hits / manual_total if manual_total else None,
        "manual_exact_turns": sum(t["score"]["manual_exact"] for t in turns),
        "unexpected_manual_count": sum(len(t["score"]["unexpected_manuals"]) for t in turns),
        "latency_median_seconds": median(latencies) if latencies else None,
        "latency_samples": len(latencies),
        "error_turns": sum(t["score"]["error"] for t in turns),
        "fallback_turns": sum(t.get("report_mode") == "source_fallback" for t in turns),
    }
