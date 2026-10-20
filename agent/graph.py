"""v2 Supervisor — 실제 langgraph.StateGraph (Wave 4·5·6·7·8·10b·15 통합).

route → (chat 직행 | incident 파이프라인)
incident: check_target(대상 확인, 불명확하면 질문 후 종료)
          → plan(MCP Prompt 절차 반영) → gate(권위 게이트) → research(가드+병렬+재시도)
          → replan_check(부족 시 gap task 후 재조사) → analysis(needs_analysis=True일 때만
          실제 실행) → write → validate(반려 시 재작성) → finalize

모든 노드는 SupervisorDeps를 functools.partial로 주입받는 순수 함수다(Wave 13).
각 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.
"""
import asyncio
import re
import time
from functools import partial
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

from .deps import SupervisorDeps
from .incident_rules import assess_services
from .retry import with_retry
from .reporting import default_step_ids, render_report, report_is_grounded
from .targets import CLARIFICATION_PREFIX, SYSTEM_NAME, extract_targets, load_known_systems, pending_query


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    route: str
    targets: list[str]
    target_status: str
    needs_analysis: bool
    tasks: list[dict]
    gate_removed: int
    collected: list[dict]
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: str
    report_step_ids: list[str]
    report_mode: str
    validation_attempts: int
    validation_feedback: str
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]


def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)


@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 — 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다."""
    # 되물음에 서비스명만 답해도 이전 장애 요청의 진단을 이어간다.
    is_target_reply = False
    if pending_query(state.get("history", [])):
        try:
            found, unknown = extract_targets(state["query"], load_known_systems())
        except (OSError, ValueError):
            found, unknown = [], []
        is_target_reply = bool(found or unknown)
    if is_target_reply:
        result, usage = {"route": "incident", "needs_analysis": False}, {"usd": 0.0}
    else:
        result, usage = deps.fast_llm.route(state["query"])
    event = {"type": "RouteDecided", "route": result["route"]}
    return {"route": result["route"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage),
            "events": state.get("events", []) + [event]}


@traceable(name="check_target_node")
async def check_target_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """MCP 조회 전에 명시된 대상을 검사하고, 불명확하면 진단을 멈춘다."""
    try:
        known = load_known_systems()
        targets, unknown = extract_targets(state["query"], known)
    except (OSError, ValueError):
        status = "unavailable"
        targets = []
        reply = "모니터링 대상 목록을 읽을 수 없어 진단을 진행할 수 없습니다. 설정을 확인해주세요."
    else:
        choices = ", ".join(sorted(known))
        if unknown:
            status = "unknown"
            reply = (f"{CLARIFICATION_PREFIX}\n등록된 모니터링 대상에서 {', '.join(unknown)}를 찾을 수 없습니다. "
                     f"서비스명을 확인해주세요.\n조회 가능한 대상: {choices}")
        elif not targets:
            status = "missing"
            reply = (f"{CLARIFICATION_PREFIX}\n어느 서비스의 응답이 느리거나 장애가 발생했나요? "
                     f"서비스명을 알려주세요.\n조회 가능한 대상: {choices}")
        else:
            status, reply = "valid", None
    event = {"type": "TargetChecked", "status": status, "targets": targets}
    update = {"targets": targets, "target_status": status,
              "events": state["events"] + [event]}
    if reply:
        update.update(final=reply, tasks=[])
    else:
        previous = pending_query(state.get("history", []))
        # 서비스명만 답한 경우 이전 증상을 유지한다. 이전의 잘못된 대상명은 제거한다.
        remainder = state["query"]
        for target in targets:
            remainder = re.sub(re.escape(target), "", remainder, flags=re.I)
        if previous and len(remainder.strip(" .,!?:은는이가요")) <= 3:
            symptom = SYSTEM_NAME.sub("", previous)
            update["query"] = f"{state['query']}\n이전 요청의 증상: {symptom}"
    return update


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 direct 경로 — 히스토리 기반 응답 (Wave 12 세션 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct"}]}


@traceable(name="plan_node")
async def plan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 4 — Plan/Task 분해. Wave 2의 MCP Prompt가 표준 절차를 깔아준다.

    resource와 마찬가지로 이름을 어느 서버가 들고 있는지 미리 가정하지 않고,
    find_prompt()로 연결된 서버들을 먼저 조회해 찾은 뒤에만 get_prompt를 보낸다."""
    prompt_server = await deps.mcp.find_prompt("incident_analysis_prompt")
    procedure = await deps.mcp.get_prompt(
        "incident_analysis_prompt",
        {"system_name": state["targets"][0], "symptom": state["query"]},
        server=prompt_server,
    )
    result, usage = deps.fast_llm.plan(state["query"], procedure)
    # 모델이 다른 서비스를 추가하거나 명시된 대상을 누락해도 실제 조회는 검증된 대상만 한다.
    # 매뉴얼도 모델의 추측이 아니라 실제 관측 결과에서 서비스별로 결정한다.
    other_tasks = [t for t in result["tasks"] if t["agent"] != "research"]
    result["tasks"] = [
        {"id": f"target-monitoring-{i}", "agent": "research", "kind": "monitoring",
         "target": target, "status": "pending"}
        for i, target in enumerate(state["targets"])
    ] + other_tasks
    targets = [t["target"] for t in result["tasks"]
               if t["agent"] == "research" and t.get("target")]
    event = {"type": "PlanCreated", "task_count": len(result["tasks"]),
             "targets": targets}
    return {"tasks": result["tasks"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 — 권위 게이트. needs_analysis=False인데 섞여 들어온 analysis task를
    프롬프트가 아니라 코드로 강제 제거한다."""
    tasks = state["tasks"]
    if not state.get("needs_analysis", False):
        kept = [t for t in tasks if t["agent"] != "analysis"]
        removed = len(tasks) - len(kept)
    else:
        kept, removed = tasks, 0
    event = {"type": "GateApplied", "removed_analysis_tasks": removed}
    return {"tasks": kept, "gate_removed": removed,
            "events": state["events"] + [event]}


@traceable(name="research_node")
async def research_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 15 가드 + Wave 10b 병렬 + Wave 9 재시도 + Wave 2 Tool/Resource 실호출."""
    tasks = state["tasks"]
    research_indices = [i for i, t in enumerate(tasks)
                        if t["agent"] == "research" and t["status"] == "pending"]
    # Wave 15 — 객체가 아니라 '실행할 목록'을 검사한다 (not research_agent 버그의 수정판)
    if not research_indices:
        return {"events": state["events"] +
                [{"type": "ResearchSkipped", "reason": "pending research task 0개"}]}

    retry_total = state.get("retry_count", 0)

    # URI를 코드에 하드코딩하지 않는다. 여러 MCP 서버가 연결돼 있고 그중 어느 서버가
    # "get_incident_runbook" resource를 제공하는지 모른다고 가정하고, 먼저 연결된
    # 서버들을 조회(list)해 유효한 서버를 찾은 뒤에만 그 서버로 요청한다.
    needs_runbook = any(tasks[i]["kind"] != "monitoring" for i in research_indices)
    runbook_server = runbook_uri_template = None
    if needs_runbook:
        runbook_server, runbook_uri_template = await deps.mcp.find_resource_template(
            "get_incident_runbook")
    started = time.perf_counter()
    # 같은 유형이 여러 서비스에 적용돼도 MCP 원문 조회는 한 번만 수행한다.
    incident_types = list(dict.fromkeys(tasks[i]["target"] for i in research_indices
                                       if tasks[i]["kind"] != "monitoring"))
    bodies = await asyncio.gather(*(
        deps.mcp.read_resource(runbook_uri_template.format(incident_type=incident_type), server=runbook_server)
        for incident_type in incident_types))
    runbook_bodies = dict(zip(incident_types, bodies))

    async def run_one(idx: int) -> dict:
        task = tasks[idx]
        await asyncio.sleep(0.35)   # 태스크당 고정 지연 — 병렬 효과를 실측하기 위함
        if task["kind"] == "monitoring":
            data, retries = await with_retry(
                deps.mcp.call_tool, "get_monitoring_status",
                {"system_name": task["target"]})
            return {"idx": idx, "source": "monitoring", "data": data, "retries": retries}
        data = runbook_bodies[task["target"]]
        return {"idx": idx, "source": "runbook", "data": data, "retries": 0}

    results = await asyncio.gather(*(run_one(i) for i in research_indices))
    elapsed = round(time.perf_counter() - started, 2)

    new_tasks = [dict(t) for t in tasks]
    collected = list(state.get("collected", []))
    events = list(state["events"])
    for r in results:
        new_tasks[r["idx"]]["status"] = "done"
        source = {"source": r["source"], "data": r["data"]}
        if r["source"] == "runbook":
            source.update(system_name=new_tasks[r["idx"]]["system_name"],
                          incident_type=new_tasks[r["idx"]]["target"])
        collected.append(source)
        retry_total += r["retries"]
        events.append({"type": "TaskCompleted", "id": new_tasks[r["idx"]]["id"],
                       "source": r["source"], "target": new_tasks[r["idx"]]["target"],
                       "system_name": new_tasks[r["idx"]].get("system_name"),
                       "retries": r["retries"]})
    return {"tasks": new_tasks, "collected": collected, "retry_count": retry_total,
            "parallel_elapsed": elapsed, "events": events}


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """서비스·장애 유형별 누락 매뉴얼을 모두 수집한다. 정상 서비스에는 강제 적용하지 않는다."""
    collected = [c for c in state.get("collected", []) if c["source"] != "assessment"]
    assessments = assess_services(collected)
    present = {(c.get("system_name"), c.get("incident_type")) for c in collected
               if c["source"] == "runbook"}
    missing = [(a["data"]["system_name"], incident_type)
               for a in assessments for incident_type in a["data"]["incident_types"]
               if (a["data"]["system_name"], incident_type) not in present]
    rounds = state.get("replan_rounds", 0)
    if not missing:
        verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": True}
        return {"collected": collected + assessments, "events": state["events"] + [verdict]}
    gap_tasks = [{"id": f"task-runbook-{rounds}-{i}", "agent": "research", "kind": "runbook",
                  "target": incident_type, "system_name": system_name, "status": "pending"}
                 for i, (system_name, incident_type) in enumerate(missing)]
    verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
               "targets": [{"system_name": s, "incident_type": t} for s, t in missing]}
    return {"tasks": state["tasks"] + gap_tasks, "collected": collected + assessments,
            "replan_rounds": rounds + 1,
            "events": state["events"] + [verdict]}


@traceable(name="analysis_node")
async def analysis_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 보완 — needs_analysis=True가 정당한 경우(모델의 자기모순이 아니라
    실제로 패턴 비교가 필요하다고 판단한 경우)를 위한 실행 경로. gate가 살려둔
    agent=='analysis' task가 있을 때만 수집된 monitoring 결과들을 비교 분석한다."""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["agent"] == "analysis" and t["status"] == "pending"]
    if not pending:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "analysis task 0개"}]}
    summary, usage = deps.fast_llm.analyze(state.get("collected", []))
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["agent"] == "analysis" and t["status"] == "pending":
            t["status"] = "done"
    collected = state.get("collected", []) + [{"source": "analysis", "data": summary}]
    event = {"type": "AnalysisCompleted", "length": len(summary)}
    return {"tasks": new_tasks, "collected": collected,
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """매뉴얼 항목 선택과 보고서 조립. 검증 전 초안은 사용자에게 보내지 않는다."""
    attempt = state.get("validation_attempts", 0) + 1
    draft, usage = await deps.quality_llm.write(
        state["query"], state.get("collected", []),
        attempt, state.get("validation_feedback"))
    return {"draft": draft, "last_prompt": usage.get("prompt"),
            "report_step_ids": usage.get("report_step_ids", []),
            "report_mode": usage.get("report_mode", "unverified"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt,
                                          "length": len(draft)}]}


@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """관측값과 매뉴얼 원문으로 조립한 보고서와 대조해 잘못된 최종 초안을 차단한다."""
    draft = state["draft"]
    attempts = state.get("validation_attempts", 0) + 1
    ok = report_is_grounded(draft, state.get("collected", []), state.get("report_step_ids", []))
    verdict = "합격" if ok else "불합격"
    replacement = {}
    if not ok and attempts >= 3:
        # 재작성 한도에 도달해도 잘못된 초안을 통과시키지 않는다.
        selected = default_step_ids(state.get("collected", []))
        replacement = {"draft": render_report(state.get("collected", []), selected),
                       "report_step_ids": selected, "report_mode": "source_fallback"}
        ok, verdict = True, "원문 보고서로 대체"
    feedback = None if ok else (
        "보고서가 수집된 관측값 및 선택한 매뉴얼 원문과 일치하지 않습니다. "
        "실제 존재하는 매뉴얼 항목을 고르고 관측값과 원문을 변경하지 마세요.")
    event = {"type": "Validation", "attempt": attempts, "verdict": verdict,
             "fallback": bool(replacement) or state.get("report_mode") == "source_fallback"}
    return {"validation_attempts": attempts, "validation_feedback": feedback,
            "events": state["events"] + [event], **replacement}


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    return {"final": state["draft"],
            "events": state["events"] + [{"type": "Finalize", "pipeline": "incident-v2"}]}


def _after_route(state: AgentState) -> str:
    return "chat_answer" if state["route"] == "chat" else "check_target"


def _after_target_check(state: AgentState) -> str:
    return "plan" if state["target_status"] == "valid" else "end"


def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    return "analysis" if last.get("is_sufficient") else "research"


def _after_validate(state: AgentState) -> str:
    return "finalize" if state.get("validation_feedback") is None else "write"


def build_graph(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    g.add_node("route", node(route_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node("check_target", node(check_target_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("research", node(research_node))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("route")
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "check_target": "check_target"})
    g.add_conditional_edges("check_target", _after_target_check,
                            {"plan": "plan", "end": END})
    g.add_edge("chat_answer", END)
    g.add_edge("plan", "gate")
    g.add_edge("gate", "research")
    g.add_edge("research", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "research": "research"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile()
