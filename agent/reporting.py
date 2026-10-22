"""모델은 매뉴얼 항목을 선택하고, 보고서는 수집 근거와 원문으로 조립한다.

자유 문장의 의미를 정규식으로 완전히 검증할 수 없으므로, 최종 보고에 허용되는
내용을 모니터링 원값·고정된 한계 설명·조회한 매뉴얼 원문으로 제한한다.
"""
import json
import re

from .incident_rules import assess_services


def runbook_steps(collected: list[dict]) -> dict[str, dict]:
    steps = {}
    systems = {c["data"]["system_name"] for c in collected if c["source"] == "monitoring"}
    for index, item in enumerate((c for c in collected if c["source"] == "runbook"), 1):
        body = item["data"].strip()
        title = next((line.lstrip("# ") for line in body.splitlines() if line.startswith("#")),
                     "대응 매뉴얼")
        # 줄바꿈 뒤에 조건이 이어져도 해당 항목 전체를 보존한다.
        pattern = r"^[ \t]*(\d+)\.[ \t]+([\s\S]*?)(?=^[ \t]*\d+\.[ \t]+|^#|\Z)"
        for match in re.finditer(pattern, body, re.M):
            step_id = f"R{index}-S{match[1]}"
            steps[step_id] = {"title": title, "number": match[1], "text": match[2].strip(),
                              "runbook_id": f"R{index}",
                              "system_name": item.get("system_name") or (
                                  next(iter(systems)) if len(systems) == 1 else "대상 서비스 미지정")}
    return steps


def step_groups(steps: dict[str, dict]) -> dict[str, list[str]]:
    groups = {}
    for step_id, step in steps.items():
        groups.setdefault(step["runbook_id"], []).append(step_id)
    return groups


def default_step_ids(collected: list[dict]) -> list[str]:
    # 전체에서 세 개만 고르면 뒤에 있는 서비스의 권고가 다시 누락될 수 있다.
    return [step_id for group in step_groups(runbook_steps(collected)).values() for step_id in group[:3]]


def valid_selection(selected, collected: list[dict]) -> bool:
    available = runbook_steps(collected)
    if (not isinstance(selected, list)
            or any(not isinstance(s, str) or s not in available for s in selected)
            or len(set(selected)) != len(selected)):
        return False
    return all(1 <= len(set(group).intersection(selected)) <= 3
               for group in step_groups(available).values())


def observation_lines(collected: list[dict]) -> list[str]:
    lines = []
    notes = {a["data"]["system_name"]: a["data"]["note"] for a in assess_services(collected)}
    notes.update({a["data"]["system_name"]: a["data"]["note"] for a in collected if a["source"] == "assessment"})
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
        if notes.get(data["system_name"]):
            lines.append("  확인 범위: " + notes[data["system_name"]])
    return lines or ["- 모니터링 데이터가 수집되지 않았습니다."]


def selection_prompt(query: str, collected: list[dict], feedback: str | None = None):
    steps = runbook_steps(collected)
    groups = step_groups(steps)
    system = (
        '당신은 ICT 운영 대응 매뉴얼의 관련 항목을 고르는 담당자다. '
        '각 서비스·매뉴얼마다 관련 항목 ID를 최소 1개, 최대 3개 고르라. '
        '서비스나 매뉴얼을 하나라도 빠뜨리면 안 된다. 각 항목의 system_name을 확인하라. '
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
                     "minItems": len(groups), "maxItems": sum(min(3, len(g)) for g in groups.values()),
                     "uniqueItems": True}},
        "required": ["step_ids"], "additionalProperties": False}
    return system, user, schema


def decode_selection(content: str, collected: list[dict]) -> list[str]:
    raw = json.loads(content)
    selected = raw.get("step_ids") if isinstance(raw, dict) else None
    if not valid_selection(selected, collected):
        raise ValueError("매뉴얼 항목 ID가 잘못됐거나 서비스·매뉴얼별 권고가 누락됐습니다.")
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
    for group in step_groups(steps).values():
        group_selected = [step_id for step_id in selected if step_id in group]
        if not group_selected:
            continue
        lines.append(f"- 대상 서비스: {steps[group_selected[0]]['system_name']}")
        for step_id in group_selected:
            step = steps[step_id]
            lines.append(f"  - [{step['title']} / {step['number']}번] {step['text']}")
    if not selected:
        assessments = assess_services(collected)
        if assessments and all(a["data"]["status"] == "no_signal" for a in assessments):
            lines.append("- 현재 관측에서는 적용할 장애 매뉴얼이 없습니다. 과거 이상 여부는 추가 확인이 필요합니다.")
        else:
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
    if not valid_selection(selected, collected):
        return False
    try:
        return draft == render_report(collected, selected)
    except ValueError:
        return False
