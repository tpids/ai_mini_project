"""사용자의 표현 대신 서비스별 관측값·알림으로 적용할 매뉴얼 유형을 판별한다."""

ALERT_RULES = [
    (("latency", "pool"), "high-latency"),
    (("cpu",), "cpu-spike"),
    (("memory", "heap"), "memory-leak"),
    (("timeout", "network"), "network-timeout"),
    (("disk",), "disk-full"),
    (("db down", "database down"), "db-down"),
]


def classify_incidents(data: dict) -> list[str]:
    alerts = " ".join(data.get("alerts", [])).casefold()
    types = [incident for keywords, incident in ALERT_RULES
             if any(keyword in alerts for keyword in keywords)]
    for field, incident in [("cpu_percent", "cpu-spike"), ("memory_percent", "memory-leak")]:
        value = data.get(field)
        if isinstance(value, (int, float)) and value >= 90 and incident not in types:
            types.append(incident)
    if data.get("status") == "down" and "db-down" not in types:
        types.append("db-down")
    return types


def assess_services(collected: list[dict]) -> list[dict]:
    assessments = []
    for item in collected:
        if item["source"] != "monitoring":
            continue
        data = item["data"]
        types = classify_incidents(data)
        if types:
            status = "matched"
            note = ""
        elif data.get("status") == "healthy" and not data.get("alerts"):
            status = "no_signal"
            note = ("현재 관측에서 분류 가능한 장애 징후가 없어 장애 대응 매뉴얼을 적용하지 않았습니다. "
                    "과거 급증이나 일시적 장애 여부는 시계열 확인이 필요합니다.")
        else:
            status = "unclassified"
            note = "수집 정보만으로 적용할 매뉴얼 유형을 판별할 수 없습니다. 추가 지표·알림을 확인해주세요."
        assessments.append({"source": "assessment", "data": {
            "system_name": data["system_name"], "incident_types": types,
            "status": status, "note": note}})
    return assessments
