"""평가자도 오류를 놓칠 수 있음을 확인하는 재현 가능한 개발용 대조 예제."""
from datetime import datetime
import json
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

from agent.incident_rules import assess_services
from agent.reporting import default_step_ids, render_report
from .judge import CRITERIA, OllamaJudge, PROMPT_VERSION

ROOT = Path(__file__).resolve().parent.parent


def sanity_cases():
    facts = [{"source": "monitoring", "data": {"system_name": "payments-service", "status": "degraded",
              "cpu_percent": 97.2, "memory_percent": 57.1, "alerts": ["CPU 91% (threshold 80%)"]}},
             {"source": "runbook", "system_name": "payments-service", "incident_type": "cpu-spike",
              "data": (ROOT / "data/runbooks/cpu-spike.md").read_text()}]
    facts += assess_services(facts)
    good = render_report(facts, default_step_ids(facts))
    bad_condition = good.replace("CPU가 90%를 넘고 지속되면 해당 인스턴스를 오토스케일링 그룹에서 한 대 더 띄워 부하를 분산한다.",
        "인스턴스를 즉시 한 대 추가했다. 로그 분석을 완료했고 배포 결함이 원인으로 확인됐다.")
    return [
        {"id": "reference", "answer": good, "facts": facts, "expected": {k: 2 for k in CRITERIA}},
        {"id": "wrong_number", "answer": good.replace("CPU 97.2%", "CPU 13.8%"), "facts": facts,
         "expected": {"numbers": 0, "conditions": 2, "uncertainty": 2, "scope": 2}},
        {"id": "unverified_actions", "answer": bad_condition, "facts": facts,
         "expected": {"numbers": 2, "conditions": 0, "uncertainty": 0, "scope": 2}},
    ]


def main():
    output = ROOT / "doc/judge_checks" / (datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    output.mkdir(parents=True)
    records = []
    manifest = {"status": "running", "model": "qwen3:8b", "prompt_version": PROMPT_VERSION,
                "description": "설명용 개발 점검. 프롬프트 반례와 유사한 유형이며 독립적인 일반화 성능 평가가 아님."}
    judge = OllamaJudge()
    try:
        for case in sanity_cases():
            result = judge.evaluate("payments-service CPU 급증 원인 진단해줘", case["facts"], case["answer"])
            records.append({**case, "judgment": result})
            (output / "checks.json").write_text(json.dumps({"manifest": manifest, "results": records}, ensure_ascii=False, indent=2) + "\n")
            print(case["id"], result["status"], {k: v["score"] for k, v in result.get("criteria", {}).items()}, flush=True)
        manifest["status"] = "complete"
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        raise
    except BaseException:
        manifest["status"] = "failed"
        raise
    finally:
        (output / "checks.json").write_text(json.dumps({"manifest": manifest, "results": records}, ensure_ascii=False, indent=2) + "\n")
    print(f"평가자 자체 점검: {output}", flush=True)
    return output


if __name__ == "__main__":
    main()
