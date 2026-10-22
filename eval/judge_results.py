"""기존 평가의 저장된 답변을 별도 채점한다. 원래 점수·응답 시간을 덮어쓰지 않는다."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from statistics import mean
import uuid
from zoneinfo import ZoneInfo

import httpx

from .judge import CRITERIA, OllamaJudge, PROMPT_VERSION


def summarize_judgments(records):
    result = {}
    for variant in sorted({r["variant"] for r in records}):
        rows = [r for r in records if r["variant"] == variant]
        valid = [r for r in rows if r["judgment"]["status"] == "ok"]
        result[variant] = {"valid": len(valid), "errors": sum(r["judgment"]["status"] == "error" for r in rows),
                           "skipped": sum(r["judgment"]["status"] == "skipped" for r in rows), "criteria": {}}
        for key in CRITERIA:
            scores = [r["judgment"]["criteria"][key]["score"] for r in valid
                      if r["judgment"]["criteria"][key]["score"] is not None]
            result[variant]["criteria"][key] = {"mean": mean(scores) if scores else None, "n": len(scores)}
    return result


def write_outputs(output, manifest, records):
    summary = summarize_judgments(records)
    (output / "summary.json").write_text(json.dumps({"manifest": manifest, "summary": summary}, ensure_ascii=False, indent=2) + "\n")
    lines = ["# LLM-as-a-Judge 평가", "", f"- 상태: {manifest['status']}",
             f"- 평가자: {manifest['model']}, temperature=0, {PROMPT_VERSION}",
             "- 동일한 저장 답변·수집 근거를 평가. 채팅 답변과 기존 지표를 수정하지 않음.",
             "- 점수: 0=모순/허위 단정, 1=누락/모호함, 2=위반 없음. 아래 값은 항목별 평균(0~2).",
             "- 해당 없는 항목·평가 실패는 0점으로 변환하지 않으며 유효 n, 오류, 제외 수를 표시.", "",
             "| 코드 | 수치·상태 | 조건 보존 | 불확실성 | 서비스 연결 | 유효 / 오류 / 제외 |",
             "|---|---:|---:|---:|---:|---:|"]
    for variant, stats in summary.items():
        cells = []
        for key in CRITERIA:
            item = stats["criteria"][key]
            cells.append(f"{item['mean']:.2f} (n={item['n']})" if item["mean"] is not None else "N/A")
        lines.append(f"| {variant} | " + " | ".join(cells) + f" | {stats['valid']} / {stats['errors']} / {stats['skipped']} |")
    lines += ["", "## 해석 범위", "", "- 모델의 주관적 판단이며 사실의 정답률이나 기존 코드 검증을 대신하지 않음.",
              "- 동일 모델을 생성자와 평가자로 사용하면 자기 선호 편향이 생길 수 있음. 원문 인용·점수·이유를 함께 검토해야 함.",
              "- 인용문이 실제 답변에 없는 출력, 잘못된 점수, 시간 초과는 평가 오류로 보존.",
              "- 일반 대화·대상 확인 안내·실행 실패는 장애 보고서 채점에서 제외. 해당 기능은 기존 대상 선택 지표로 평가.",
              "- Judge 시간은 별도 기록하며 기존 응답 시간 중앙값에 더하지 않음.",
              "- 같은 고정 관측값에 대한 모델 판단의 반복 일관성은 별도 검증이 필요."]
    (output / "report.md").write_text("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="저장된 전후 보고서를 로컬 LLM으로 별도 평가")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--repeats", type=int, default=1, help="각 저장 답변의 평가 반복 수")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.timeout_seconds <= 0:
        parser.error("반복 수와 제한 시간은 양수여야 합니다.")
    source = args.run_dir / "raw_results.jsonl"
    payload = source.read_bytes()
    rows = [json.loads(s) for s in payload.decode().splitlines() if s.strip()]
    known = {r["case_id"] for r in rows}
    if set(args.case) - known:
        parser.error("존재하지 않는 case ID")
    selected = [r for r in rows if r["phase"] == "measured" and (not args.case or r["case_id"] in args.case)]
    if not selected:
        parser.error("측정된 응답이 없습니다.")
    output = args.run_dir / ("judge_" + datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    output.mkdir()
    judge = OllamaJudge(args.model, timeout=args.timeout_seconds)
    # 모델 digest도 기록하되 모델이 없거나 서버가 꺼졌어도 실패한 실행의 기록을 남긴다.
    manifest = {"status": "running", "model": args.model, "prompt_version": PROMPT_VERSION,
                "source": str(source.resolve()), "source_sha256": hashlib.sha256(payload).hexdigest(),
                "case_ids": sorted({r['case_id'] for r in selected}), "repeats": args.repeats,
                "generation_backend": json.loads((args.run_dir / "summary.json").read_text())["manifest"]["backend"]}
    try:
        tags = httpx.get(judge.base_url + "/api/tags", timeout=5)
        tags.raise_for_status()
        name = args.model if ":" in args.model else args.model + ":latest"
        manifest["model_digest"] = next((m["digest"] for m in tags.json()["models"] if m["name"] == name), None)
    except Exception as exc:
        manifest["model_metadata_error"] = type(exc).__name__
    records = []
    write_outputs(output, manifest, records)
    try:
        for round_ in range(args.repeats):
            # 매번 특정 버전만 먼저 채점하지 않도록 저장 순서를 번갈아 적용한다.
            for row in selected if round_ % 2 == 0 else reversed(selected):
                for index, turn in enumerate(row["turns"]):
                    print(f"judge {round_ + 1}/{args.repeats}: {row['variant']} / {row['case_id']} / turn {index + 1}", flush=True)
                    if turn.get("error") or not turn.get("collected") or not any(c["source"] == "monitoring" for c in turn["collected"]):
                        judgment = {"status": "skipped", "reason": "실패 또는 수집 근거가 있는 장애 보고서가 아님"}
                    else:
                        judgment = judge.evaluate(turn["expected"]["query"], turn["collected"], turn["reply"])
                    record = {"variant": row["variant"], "case_id": row["case_id"], "source_repeat": row["repeat"],
                              "turn": index + 1, "judge_repeat": round_ + 1, "judgment": judgment}
                    records.append(record)
                    with (output / "raw_judgments.jsonl").open("a") as file:
                        file.write(json.dumps(record, ensure_ascii=False) + "\n")
        manifest["status"] = "complete"
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        raise
    except BaseException:
        manifest["status"] = "failed"
        raise
    finally:
        write_outputs(output, manifest, records)
    print(f"Judge 평가 결과: {output}", flush=True)
    return output


if __name__ == "__main__":
    main()
