"""모델은 매뉴얼 항목을 선택하고, 보고서는 수집 근거와 원문으로 조립한다.

자유 문장의 의미를 정규식으로 완전히 검증할 수 없으므로, 최종 보고에 허용되는
내용을 모니터링 원값·고정된 한계 설명·조회한 매뉴얼 원문으로 제한한다.
"""
import json
import re


def runbook_steps(collected: list[dict]) -> dict[str, dict]:
    steps = {}
    for index, item in enumerate((c for c in collected if c["source"] == "runbook"), 1):
        body = item["data"].strip()
        title = next((line.lstrip("# ") for line in body.splitlines() if line.startswith("#")),
                     "대응 매뉴얼")
        # 줄바꿈 뒤에 조건이 이어져도 해당 항목 전체를 보존한다.
        pattern = r"^[ \t]*(\d+)\.[ \t]+([\s\S]*?)(?=^[ \t]*\d+\.[ \t]+|^#|\Z)"
        for match in re.finditer(pattern, body, re.M):
            step_id = f"R{index}-S{match[1]}"
            steps[step_id] = {"title": title, "number": match[1], "text": match[2].strip()}
    return steps


def default_step_ids(collected: list[dict]) -> list[str]:
    return list(runbook_steps(collected))[:3]


def observation_lines(collected: list[dict]) -> list[str]:
    lines = []
    for item in collected:
        if item["source"] != "monitoring":
            continue
        data = item["data"]
        metrics = [f"상태={data.get('status', '미수집')}"]
        for key, label in [("cpu_percent", "CPU"), ("memory_percent", "메모리")]:
            if key in data:
                metrics.append(f"{label} {data[key]}%")
        lines.append(f"- {data['system_name']}: {', '.join(metrics)}")
        if data.get("last_checked"):
            lines.append(f"  조회 시각: {data['last_checked']}")
        if "alerts" in data:
            lines.append("  알림 원문: " + json.dumps(data["alerts"], ensure_ascii=False))
    return lines or ["- 모니터링 데이터가 수집되지 않았습니다."]


def selection_prompt(query: str, collected: list[dict], feedback: str | None = None):
    steps = runbook_steps(collected)
    system = (
        '당신은 ICT 운영 대응 매뉴얼의 관련 항목을 고르는 담당자다. '
        '사용자 요청과 관측값에 관련된 항목 ID를 1~3개 고르라. '
        '확인 단계와 조건부 조치를 함께 고려하라. '
        '매뉴얼은 참고 절차이며, 조건 충족·작업 실행·원인 확인의 증거가 아니다. '
        '답변 문장이나 새 수치·인스턴스명을 만들지 마라. '
        'JSON 객체만 응답하라. 형식: {"step_ids": ["R1-S1", "R1-S3"]}. '
        '아래 목록에 실제 존재하는 ID만 사용하라.'
    )
    user = (f"## 사용자 요청\n{query}\n\n## 관측값\n" + "\n".join(observation_lines(collected))
            + "\n\n## 선택 가능한 매뉴얼 항목\n" + json.dumps(steps, ensure_ascii=False))
    if feedback:
        user += f"\n\n## 이전 검증 피드백\n{feedback}"
    schema = {"type": "object", "properties": {
        "step_ids": {"type": "array", "items": {"type": "string", "enum": list(steps)},
                     "minItems": 1, "maxItems": 3, "uniqueItems": True}},
        "required": ["step_ids"], "additionalProperties": False}
    return system, user, schema


def decode_selection(content: str, collected: list[dict]) -> list[str]:
    raw = json.loads(content)
    selected = raw.get("step_ids") if isinstance(raw, dict) else None
    available = runbook_steps(collected)
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 3
            or any(not isinstance(s, str) or s not in available for s in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError("매뉴얼 항목 ID가 유효하지 않습니다.")
    return selected


def render_report(collected: list[dict], selected: list[str]) -> str:
    steps = runbook_steps(collected)
    if any(step_id not in steps for step_id in selected):
        raise ValueError("조회한 매뉴얼에 없는 항목입니다.")
    lines = ["1) 확인된 사실", *observation_lines(collected), "", "2) 아직 확인하지 못한 내용",
             "- 관측값만으로 근본 원인을 확정할 수 없습니다. 시계열과 지속 여부는 검증하지 않았습니다.",
             "- 최근 로그·배포 이력·리전별 분포는 수집하지 않았습니다. 패턴 분석 결과만으로 이상이 없다고 판단하지 않습니다.",
             "", "3) 매뉴얼에 따른 점검·조치 권고",
             "- 아래 절차는 실행한 작업이 아닙니다. 조건 충족 여부를 추가 확인한 뒤 적용을 검토하세요. 매뉴얼의 임계값은 관측값과 구분해야 합니다."]
    for step_id in selected:
        step = steps[step_id]
        lines.append(f"- [{step['title']} / {step['number']}번] {step['text']}")
    if not selected:
        lines.append("- 적용할 매뉴얼 항목이 수집되지 않았습니다. 관련 매뉴얼과 추가 근거를 확인해주세요.")
    return "\n".join(lines)


def selected_report(content: str, collected: list[dict], usage: dict) -> tuple[str, dict]:
    try:
        selected = decode_selection(content, collected)
        mode = "source_selected"
    except (ValueError, TypeError):
        selected = default_step_ids(collected)
        mode = "source_fallback"
    return render_report(collected, selected), {**usage, "report_step_ids": selected,
                                                "report_mode": mode}


def report_is_grounded(draft: str, collected: list[dict], selected: list[str]) -> bool:
    """원문의 조건과 수치를 보존한 보고서만 통과시킨다."""
    if not isinstance(selected, list) or any(not isinstance(s, str) for s in selected):
        return False
    available = runbook_steps(collected)
    if len(selected) > 3 or len(set(selected)) != len(selected) or (available and not selected):
        return False
    try:
        return draft == render_report(collected, selected)
    except ValueError:
        return False
