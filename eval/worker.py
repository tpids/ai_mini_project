"""지정한 코드의 agent를 별도 프로세스에서 불러와 JSON 행으로 평가한다."""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import time


class FixtureMCP:
    """MCP 경계만 고정 입력으로 대체한다. 운영 서버를 호출하지 않는다."""
    def __init__(self, source_root: Path):
        self.root = source_root
        self.monitoring = {}
        self.reset_calls()

    def reset_calls(self):
        self.monitoring_targets = []
        self.runbook_types = []

    async def find_prompt(self, name):
        return "fixture-ops"

    async def get_prompt(self, name, arguments, server):
        return (
            "다음 장애에 대해 표준 진단 절차를 수행하세요.\n\n"
            f"## 장애 정보\n- 시스템: {arguments['system_name']}\n- 증상: {arguments['symptom']}\n\n"
            "## 진단 절차 (반드시 순서대로 수행)\n"
            "1. get_monitoring_status Tool로 관련 시스템의 현재 상태를 조회하세요.\n"
            "2. 증상에 해당하는 runbook(runbook://incident-response/...)을 확인하세요.\n"
            "3. 위 정보를 종합해 가능한 원인과 권장 조치를 제시하세요.\n")

    async def call_tool(self, name, arguments):
        if name != "get_monitoring_status":
            raise ValueError(f"평가 fixture가 지원하지 않는 tool: {name}")
        target = arguments["system_name"]
        self.monitoring_targets.append(target)
        return copy.deepcopy(self.monitoring.get(target, {
            "system_name": target, "status": "unknown", "cpu_percent": 0.0,
            "memory_percent": 0.0, "last_checked": "2026-10-21T00:00:00+09:00",
            "alerts": [f"System '{target}' not found in monitoring"],
        }))

    async def find_resource_template(self, name):
        return "fixture-ops", "runbook://incident-response/{incident_type}"

    async def read_resource(self, uri, server):
        kind = uri.rsplit("/", 1)[-1]
        # 리소스 이름으로 임의 파일 경로를 읽지 않는다.
        if not kind or any(c not in "abcdefghijklmnopqrstuvwxyz-" for c in kind):
            raise ValueError("유효하지 않은 매뉴얼 유형")
        path = self.root / "data" / "runbooks" / f"{kind}.md"
        if not path.is_file():
            return f"# Runbook Not Found\n\nNo runbook for incident type: {kind}"
        self.runbook_types.append(kind)
        return path.read_text()


async def serve(args):
    # 이전 코드와 현재 코드의 agent 모듈을 섞지 않는다.
    sys.path.insert(0, str(args.source_root))
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    from agent.deps import SupervisorDeps
    from agent.graph import build_graph
    if args.backend == "fake":
        from agent.fake_llm import FastLLM, QualityLLM
        fast, quality = FastLLM("fake-eval-fast", 0), QualityLLM("fake-eval-quality", 0)
    else:
        from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
        fast = OllamaFastLLM(args.fast_model, 0)
        quality = OllamaQualityLLM(args.quality_model, 0)
    fixture = FixtureMCP(args.source_root)
    deps = SupervisorDeps(fast, quality, fixture, None, "v2")
    if args.retrieval == "rag":
        from agent.retrieval import build_retriever
        deps.runbook_retriever = build_retriever(args.source_root, args.rag_db)
    graph = build_graph(deps)
    print(json.dumps({"ready": True, "fast_model": fast.name, "quality_model": quality.name}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        case = request["case"]
        fixture.monitoring = copy.deepcopy(request["monitoring"])
        fixture.monitoring.update(copy.deepcopy(case.get("monitoring_overrides", {})))
        history, turns = [], []
        for index, expected in enumerate(case["turns"]):
            fixture.reset_calls()
            started = time.perf_counter()
            try:
                result = await graph.ainvoke({
                    "query": expected["query"], "history": list(history), "events": [],
                    "thread_id": f"eval-{case['id']}-{index}", "cost_usd": 0.0})
                actual = {"route": result.get("route"), "reply": result["final"],
                          "report_mode": result.get("report_mode"), "events": result.get("events", []),
                          "collected": result.get("collected", []),
                          "service_manuals": [{"system_name": c.get("system_name"),
                                               "incident_type": c.get("incident_type")}
                                              for c in result.get("collected", []) if c["source"] == "runbook"],
                          "error": None}
                history.extend([{"role": "user", "content": expected["query"]},
                                {"role": "assistant", "content": result["final"]}])
            except Exception as exc:
                actual = {"error": f"{type(exc).__name__}: {exc}", "reply": "", "route": None}
            actual.update(elapsed_seconds=time.perf_counter() - started,
                          monitoring_targets=list(fixture.monitoring_targets),
                          runbook_types=list(fixture.runbook_types))
            turns.append(actual)
        print(json.dumps({"turns": turns}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--retrieval", choices=["rules", "rag"], default="rules")
    parser.add_argument("--rag-db", type=Path)
    parser.add_argument("--backend", choices=["fake", "ollama"], required=True)
    parser.add_argument("--fast-model", default="qwen3:0.6b")
    parser.add_argument("--quality-model", default="qwen3:8b")
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
