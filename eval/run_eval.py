"""고정 사례로 과거 커밋과 현재 작업 트리의 반복 평가를 실행하는 CLI."""
import argparse
import csv
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import platform
import queue
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid
from zoneinfo import ZoneInfo

from .metrics import score_turn, summarize

ROOT = Path(__file__).resolve().parent.parent


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def fingerprint(root):
    files = sorted([*root.joinpath("agent").glob("*.py"),
                    *root.joinpath("data").glob("*.json"),
                    *root.joinpath("data/runbooks").glob("*.md")])
    hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return {"sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
            "files": hashes}


def export_baseline(ref, destination):
    revision = git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    archive = git("archive", "--format=tar", revision, "agent", "data")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] not in {"agent", "data"}:
                raise ValueError("허용되지 않은 Git snapshot 경로")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError("평가 snapshot에는 일반 파일만 허용합니다.")
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
    return revision


class Worker:
    def __init__(self, source_root, args, log_path):
        self.responses = queue.Queue()
        self.log = log_path.open("a")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "eval.worker", "--source-root", str(source_root),
             "--backend", args.backend, "--fast-model", args.fast_model, "--quality-model", args.quality_model],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, bufsize=1)
        def read():
            for line in self.process.stdout:
                try:
                    self.responses.put(json.loads(line))
                except ValueError:
                    self.responses.put({"protocol_error": line[:500]})
            self.responses.put({"protocol_error": "평가 프로세스가 종료됐습니다."})
        threading.Thread(target=read, daemon=True).start()
        try:
            self.info = self.receive(30)
            if not self.info.get("ready"):
                raise RuntimeError("평가 프로세스 초기화 실패")
        except BaseException:
            self.close()
            raise

    def receive(self, timeout):
        try:
            response = self.responses.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"평가 제한 시간 {timeout}초 초과") from exc
        if "protocol_error" in response:
            raise RuntimeError(response["protocol_error"])
        return response

    def run(self, case, monitoring, timeout):
        self.process.stdin.write(json.dumps({"case": case, "monitoring": monitoring}, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        return self.receive(timeout)["turns"]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe:
                pipe.close()
        self.log.close()


def load_dataset(path, selected_ids):
    data = json.loads(path.read_text())
    ids = [c["id"] for c in data["cases"]]
    if len(set(ids)) != len(ids):
        raise ValueError("평가 case ID가 중복됩니다.")
    if set(selected_ids) - set(ids):
        raise ValueError("알 수 없는 case ID: " + ", ".join(sorted(set(selected_ids) - set(ids))))
    cases = [c for c in data["cases"] if not selected_ids or c["id"] in selected_ids]
    if not cases:
        raise ValueError("평가할 사례가 없습니다.")
    for case in cases:
        if not case.get("turns"):
            raise ValueError("각 사례에는 평가할 turn이 필요합니다.")
        for turn in case["turns"]:
            if turn["expected_route"] not in {"chat", "incident"}:
                raise ValueError("유효하지 않은 정답 route")
            for field in ("expected_targets", "expected_runbooks"):
                values = turn[field]
                if not isinstance(values, list) or any(not isinstance(v, str) for v in values) or len(set(values)) != len(values):
                    raise ValueError(f"유효하지 않은 {field}")
    return data, cases


def format_rate(value, hits, total):
    return f"{value:.1%} ({hits}/{total})" if value is not None else "N/A (분모 0)"


def write_results(output, manifest, records):
    variants = list(manifest["variants"])
    stats = {v: summarize([r for r in records if r["variant"] == v]) for v in variants}
    per_case = {v: {cid: summarize([r for r in records if r["variant"] == v and r["case_id"] == cid])
                    for cid in manifest["case_ids"]} for v in variants}
    (output / "summary.json").write_text(json.dumps({"manifest": manifest, "summary": stats,
                                                    "per_case": per_case}, ensure_ascii=False, indent=2) + "\n")
    with (output / "results.csv").open("w", newline="") as file:
        fields = ["variant", "backend", "phase", "repeat", "case_id", "turn", "query", "target_correct",
                  "manual_hits", "manual_required", "manual_exact", "unexpected_manuals",
                  "elapsed_seconds", "error", "report_mode", "expected_targets", "actual_targets"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in records:
            if row["phase"] != "measured":
                continue
            for index, turn in enumerate(row["turns"]):
                writer.writerow({"variant": row["variant"], "backend": manifest["backend"],
                                 "phase": row["phase"], "repeat": row["repeat"], "case_id": row["case_id"],
                                 "turn": index + 1, "query": turn["expected"]["query"],
                                 **{k: turn["score"][k] for k in ["target_correct", "manual_hits", "manual_required", "manual_exact"]},
                                 "unexpected_manuals": json.dumps(turn["score"]["unexpected_manuals"]),
                                 "elapsed_seconds": turn.get("elapsed_seconds"), "error": turn.get("error"),
                                 "report_mode": turn.get("report_mode"),
                                 "expected_targets": json.dumps(turn["expected"]["expected_targets"]),
                                 "actual_targets": json.dumps(turn.get("monitoring_targets", []))})
    lines = ["# 고정 입력 반복 평가", "", f"- 실행 상태: **{manifest.get('status', 'unknown')}**",
             f"- 모델 모드: **{manifest['backend']}**",
             f"- 사례 {len(manifest['case_ids'])}개, 사례별 측정 {manifest['repeats']}회, 워밍업 {manifest['warmups']}회 제외",
             "- 데이터: 고정 fixture MCP 어댑터. 실제 모니터링 서버·MCP 전송 시간은 포함하지 않음",
             "- 순서: 반복마다 이전/현재 코드의 실행 순서를 번갈아 적용", "",
             "| 코드 | 대상 선택 정확도 | 필요 매뉴얼 조회 재현율 | 응답 시간 중앙값 | 실패 turn |",
             "|---|---:|---:|---:|---:|"]
    for variant, stat in stats.items():
        latency = f"{stat['latency_median_seconds']:.3f}초 (n={stat['latency_samples']})" if stat['latency_median_seconds'] is not None else "N/A"
        lines.append(f"| {variant} | {format_rate(stat['target_accuracy'], stat['target_correct'], stat['turns'])} | "
                     f"{format_rate(stat['manual_recall'], stat['manual_hits'], stat['manual_required'])} | {latency} | {stat['error_turns']} |")
    lines += ["", "## 계산 방법과 해석 범위", "",
              "- 대상 선택: 기대 route와 실제 모니터링 대상 집합이 모두 일치해야 정답. 확인 요청 사례는 지정한 안내 문구도 확인.",
              "- 매뉴얼 조회 재현율: 기대 유형 중 성공적으로 읽은 유형 수 / 기대 유형 수. 답변 품질이나 서비스별 권고 연결률을 뜻하지 않음.",
              "- 기대 매뉴얼이 없는 turn은 재현율 분모에서 제외. 불필요한 조회는 summary.json의 unexpected_manual_count와 manual_exact_turns로 별도 기록.",
              "- 오류·시간 초과는 정확도 분모에서 제외하지 않음. 성공 응답만 시간 중앙값에 포함하고 실패 수와 표본 수를 함께 표시.",
              "- 질문별 중앙값은 summary.json의 per_case에 기록. 전체 중앙값은 서로 다른 질문을 합친 해당 평가 묶음의 값.",
              "- 프로세스 시작·그래프 구성·워밍업은 측정 제외. 그래프 내부의 기존 인위적 지연은 포함.",
              "- 고정 데이터와 현재 모델을 사용한 제한된 회귀 평가. 운영 장애 전체의 정확도나 실제 서비스 지연으로 일반화하지 않음.",
              "- 원문 검증을 답변 전체의 의미적 정확도 점수나 LLM-as-judge 평가로 바꾸어 부르지 않음."]
    if manifest["backend"] == "fake":
        lines += ["", "**fake 결과는 모의 모델의 결정론적 흐름 검증이며 실제 LLM 성능이나 응답 시간 비교 자료가 아닙니다.**"]
    (output / "report.md").write_text("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="고정 입력으로 이전/현재 그래프의 평가 지표 비교")
    parser.add_argument("--backend", choices=["fake", "ollama"], default="fake")
    parser.add_argument("--baseline-ref", help="비교할 Git commit/ref. 생략하면 현재 코드만 평가")
    parser.add_argument("--dataset", type=Path, default=ROOT / "eval/cases.json")
    parser.add_argument("--case", action="append", default=[], help="평가할 case ID. 여러 번 지정 가능")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=300, help="사례 전체(여러 turn 포함)의 제한 시간")
    parser.add_argument("--fast-model", default="qwen3:0.6b")
    parser.add_argument("--quality-model", default="qwen3:8b")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.warmups < 0 or args.timeout_seconds <= 0:
        parser.error("repeats는 1 이상, warmups는 0 이상, 제한 시간은 양수여야 합니다.")
    dataset, cases = load_dataset(args.dataset, args.case)
    run_id = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    output = (args.output_dir or ROOT / "doc/eval_runs" / f"{run_id}_{args.backend}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    records, workers = [], {}
    with tempfile.TemporaryDirectory(prefix="ai-mini-eval-") as temp:
        sources = {}
        variants = {}
        if args.baseline_ref:
            source = Path(temp) / "baseline"
            revision = export_baseline(args.baseline_ref, source)
            sources["before"] = source
            variants["before"] = {"revision": revision, "requested_ref": args.baseline_ref,
                                  "source": "git_snapshot", "fingerprint": fingerprint(source)}
        sources["after"] = ROOT
        variants["after"] = {"revision": git("rev-parse", "HEAD").decode().strip(), "source": "working_tree",
                             "source_changes": git("status", "--porcelain", "--", "agent", "data").decode(),
                             "fingerprint": fingerprint(ROOT)}
        manifest = {"schema_version": 1, "run_id": run_id, "backend": args.backend, "status": "running",
                    "data_backend": "fixed_fixture_adapter", "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
                    "case_ids": [c["id"] for c in cases], "repeats": args.repeats, "warmups": args.warmups,
                    "timeout_seconds": args.timeout_seconds, "variants": variants,
                    "python": platform.python_version(), "timing": "graph invocation per turn; successful turns only; excludes startup and warmups"}
        (output / "dataset.json").write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n")
        write_results(output, manifest, records)
        try:
            for variant, source in sources.items():
                worker = Worker(source, args, output / f"{variant}.stderr.log")
                workers[variant] = worker
                variants[variant]["models"] = worker.info
            for phase, count in [("warmup", args.warmups), ("measured", args.repeats)]:
                for repeat in range(count):
                    order = list(sources) if repeat % 2 == 0 else list(reversed(sources))
                    for case in cases:
                        for variant in order:
                            print(f"{phase} {repeat + 1}/{count}: {variant} / {case['id']}", flush=True)
                            try:
                                if workers[variant] is None:
                                    workers[variant] = Worker(sources[variant], args, output / f"{variant}.stderr.log")
                                turns = workers[variant].run(case, dataset["monitoring"], args.timeout_seconds)
                            except Exception as exc:
                                if workers[variant]:
                                    workers[variant].close()
                                workers[variant] = None
                                turns = [{"error": f"{type(exc).__name__}: {exc}", "elapsed_seconds": None,
                                          "monitoring_targets": [], "runbook_types": []} for _ in case["turns"]]
                            for expected, turn in zip(case["turns"], turns, strict=True):
                                turn["expected"] = expected
                                turn["score"] = score_turn(expected, turn)
                            row = {"variant": variant, "phase": phase, "repeat": repeat + 1,
                                   "case_id": case["id"], "turns": turns}
                            records.append(row)
                            with (output / "raw_results.jsonl").open("a") as file:
                                file.write(json.dumps(row, ensure_ascii=False) + "\n")
            manifest["status"] = "complete"
        except KeyboardInterrupt:
            manifest["status"] = "interrupted"
            manifest["interruption"] = "사용자가 실행을 중단했습니다. 미완료 실행을 전체 평가 결과로 해석하지 마세요."
            raise
        except BaseException as exc:
            manifest["status"] = "failed"
            manifest["failure"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            for worker in workers.values():
                if worker:
                    worker.close()
            write_results(output, manifest, records)
    print(f"평가 결과: {output}", flush=True)
    return output


if __name__ == "__main__":
    main()
