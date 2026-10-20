import asyncio
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from .documents import document, review, ref, passage_map, issue_records, merge_pending_issues
from .provider import call_provider
from .mcp_client import get_passages
from .tracing import trace_span

class State(TypedDict):
    run: dict


def build_workflow(store, provider):
    def review_context(run):
        return {**run["context"], "unresolved_issues": merge_pending_issues(
            run["context"].get("unresolved_issues", []), run.get("pending_issues", []))}

    def save(run, stage, message):
        run["stage"] = stage
        store.put("run", run["id"], run)
        store.event(run, message)

    async def verify_context(run):
        mapping = passage_map(run["context"]["documents"])
        keys = [f"{dref}:{pid}" for dref, pid in mapping]
        retrieved = await asyncio.wait_for(get_passages(store.path, run["context"]["version"], keys), timeout=30)
        if not isinstance(retrieved, list) or len(retrieved) != len(keys):
            raise ValueError("MCP 기준 근거 응답이 올바르지 않습니다")
        expected = {(dref, pid, text) for (dref, pid), text in mapping.items()}
        actual = {(p["document_ref"], p["id"], p["text"]) for p in retrieved}
        if expected != actual:
            raise ValueError("MCP 기준 근거가 스냅샷과 일치하지 않습니다")
        if run["trace"]:
            await trace_span(run["usage"], "mcp/get_passages", {"transport": "stdio", "passage_count": len(retrieved), "status": "completed"})
        run["evidence_lookup"] = {"transport": "mcp-stdio", "tool": "get_passages", "passage_count": len(retrieved)}

    async def generate(state):
        run = state["run"]
        save(run, "generate", "기준 근거를 조회하고 초안을 생성합니다")
        await verify_context(run)
        raw = await call_provider(provider, "writer", "generate", {"context": run["context"], "request": run["request"]}, run["usage"], run["trace"])
        run["draft"] = document(raw, "draft", run["context"]["version"], run["context"]["documents"])
        run["warnings"].extend(run["draft"]["warnings"])
        save(run, "generated", "초안을 저장했습니다")
        return {"run": run}

    async def inspect(state):
        run = state["run"]
        doc = run["revision"] or run["draft"]
        stage = "review_revision" if run["revision"] else "review"
        save(run, stage, "본문 근거를 검토합니다")
        raw = await call_provider(provider, "reviewer", stage, {"context": review_context(run), "request": run["request"], "document": doc}, run["usage"], run["trace"])
        reviewed = review(raw, doc, run["context"]["documents"])
        run["reviews"].append(reviewed)
        run["pending_issues"] = merge_pending_issues(run.get("pending_issues", []), issue_records(run, reviewed, {"intent_question"}))
        save(run, "reviewed", "검토 결과를 저장했습니다")
        return {"run": run}

    async def revise(state):
        run = state["run"]
        save(run, "revise", "검토 의견을 반영해 한 번 수정합니다")
        raw = await call_provider(provider, "writer", "revise", {"context": review_context(run), "request": run["request"], "document": run["draft"], "protected_unresolved_issues": run.get("pending_issues", []), "review": {"document_ref": run["reviews"][-1]["document_ref"], "issues": [i for i in run["reviews"][-1]["issues"] if i["status"] == "open" and i["kind"] != "intent_question"]}}, run["usage"], run["trace"])
        run["revision"] = document(raw, "revision", run["context"]["version"], run["context"]["documents"], parent=ref(run["draft"]), document_id=run["draft"]["document_id"], version=2)
        run["warnings"].extend(run["revision"]["warnings"])
        save(run, "revised", "수정본을 저장했습니다")
        return {"run": run}

    async def route(state):
        run = state["run"]
        choice = "revise" if not run["revision"] and any(i["status"] == "open" and i["kind"] != "intent_question" for i in run["reviews"][-1]["issues"]) else "finish"
        if run["trace"]:
            await trace_span(run["usage"], "branch", {"next_stage": choice, "revision_used": bool(run["revision"])})
        return choice

    async def finish(state):
        run = state["run"]
        run["status"] = "needs_attention" if run.get("pending_issues") or any(i["status"] == "open" for i in run["reviews"][-1]["issues"]) else "completed"
        # Publish terminal status only after optional trace cleanup in execute().
        # This prevents a late worker write from overwriting a user decision.
        run["stage"] = "finished"
        return {"run": run}

    graph = StateGraph(State)
    graph.add_node("generate", generate)
    graph.add_node("review", inspect)
    graph.add_node("revise", revise)
    graph.add_node("finish", finish)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", "review")
    graph.add_conditional_edges("review", route, {"revise": "revise", "finish": "finish"})
    graph.add_edge("revise", "review")
    graph.add_edge("finish", END)
    return graph.compile()
