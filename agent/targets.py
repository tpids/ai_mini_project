"""등록된 모니터링 대상과 질문의 서비스명을 비교한다. 추측으로 대체하지 않는다."""
import json
import re
from pathlib import Path

MONITORING_PATH = Path(__file__).resolve().parent.parent / "data" / "monitoring.json"
CLARIFICATION_PREFIX = "진단할 서비스 확인이 필요합니다."
# 배포 예제의 service/호스트 명명 규칙. 한국어 조사('service가')는 허용한다.
SYSTEM_NAME = re.compile(r"(?<![a-z0-9_-])[a-z][a-z0-9_-]*-(?:service|\d+)(?![a-z0-9_-])", re.I)


def load_known_systems() -> set[str]:
    data = json.loads(MONITORING_PATH.read_text())
    if not isinstance(data, dict) or not data:
        raise ValueError("모니터링 대상 목록이 비어 있거나 올바르지 않습니다.")
    return set(data)


def extract_targets(query: str, known: set[str]) -> tuple[list[str], list[str]]:
    names = {name.casefold(): name for name in known}
    # 임의의 등록명도 지원하되 auth-service를 my-auth-service의 일부로 인식하지 않는다.
    pattern = re.compile(
        r"(?<![a-z0-9_-])(?:" + "|".join(re.escape(s) for s in sorted(known, key=len, reverse=True))
        + r")(?![a-z0-9_-])", re.I)
    matches = [(m.start(), m.group()) for m in pattern.finditer(query)]
    matches += [(m.start(), m.group()) for m in SYSTEM_NAME.finditer(query)]
    found, unknown = [], []
    for _, value in sorted(matches):
        canonical = names.get(value.casefold())
        output, name = (found, canonical) if canonical else (unknown, value.casefold())
        if name not in output:
            output.append(name)
    return found, unknown


def pending_query(history: list[dict]) -> str | None:
    """바로 직전에 대상 확인을 요청한 대화에 한해서 후속 서비스명 응답을 받는다."""
    if len(history) >= 2 and history[-1].get("role") == "assistant" and history[-1].get(
            "content", "").startswith(CLARIFICATION_PREFIX) and history[-2].get("role") == "user":
        return history[-2]["content"]
    return None
