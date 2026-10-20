"""Single-process local FastAPI application; SQLite owns durable state."""
import asyncio
import copy
import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse, Response
from dotenv import load_dotenv
from pydantic import ValidationError
from langsmith import tracing_context
from .models import SourceInput, AnalyzeInput, RunInput, DecisionInput
from .store import Store, now, new_id
from .documents import document, analyze_document, ref, graph, export_markdown, valid_evidence, issue_records, merge_pending_issues, pending_issues_length
from .provider import MockProvider, OpenAIProvider, call_provider, usage, PROMPT_VERSION
from .workflow import build_workflow
from .tracing import trace_start, trace_end

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def create_app(db_path=None, provider=None):
    path = db_path or os.getenv("LORE_DB_PATH", "runtime/loreforge.db")
    store = Store(path)
    if provider is None:
        name = os.getenv("LORE_PROVIDER", "openai")
        if name not in ("openai", "mock"):
            raise ValueError("LORE_PROVIDER는 openai 또는 mock이어야 합니다")
        provider = MockProvider() if name == "mock" else OpenAIProvider()
    gate = asyncio.Lock()
    tasks = set()

    @asynccontextmanager
    async def lifespan(app):
        store.interrupt()
        yield
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="LoreForge", lifespan=lifespan)
    app.state.store, app.state.provider = store, provider
    app.state.tasks = tasks

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        messages = [f'{".".join(str(x) for x in e["loc"] if x != "body")}: {e["msg"]}' for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": "; ".join(messages)})

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return JSONResponse(status_code=500, content={"detail": "서버 처리 중 오류가 발생했습니다. 저장된 작업 상태를 확인해주세요."})

    def required(kind, id):
        value = store.get(kind, id)
        if value is None:
            raise HTTPException(404, "자료를 찾을 수 없습니다")
        return value

    def existing_request(request_id, kind, value):
        saved = store.request(request_id)
        if saved:
            legacy_create = kind == "run" and value.get("mode") == "create" and saved["fingerprint"] == fingerprint({k: v for k, v in value.items() if k != "mode"})
            if saved["kind"] != kind or (saved["fingerprint"] != fingerprint(value) and not legacy_create):
                raise HTTPException(409, "같은 request_id가 다른 요청에 사용되었습니다")
            return required(kind, saved["target"])
        return None

    async def acquire():
        # There is no await between checking and acquiring a free asyncio.Lock.
        if gate.locked():
            raise HTTPException(409, "다른 AI 작업이 진행 중입니다. 완료 후 다시 시도해주세요.")
        await gate.acquire()

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "provider": provider.name, "key_configured": bool(os.getenv("OPENAI_API_KEY")), "models": provider.models}

    @app.post("/api/sources", status_code=201)
    async def add_source(body: SourceInput):
        if len(body.text.encode("utf-8")) > 100 * 1024:
            raise HTTPException(422, "입력 파일은 UTF-8 기준 100KB 이하로 줄여주세요")
        id = new_id("source")
        doc = document({"title": body.title, "passages": [{"id": f"p{i:03d}", "text": text} for i, text in enumerate(re.split(r'\n\s*\n', body.text), 1) if text.strip()], "entities": [], "relations": []}, "input", None)
        source = {"id": id, "text": body.text, "title": body.title, "sha256": hashlib.sha256(body.text.encode()).hexdigest(), "document": doc, "analysis": None}
        store.put("source", id, source)
        return source

    @app.get("/api/sources/{source_id}")
    async def read_source(source_id: str):
        return required("source", source_id)

    @app.post("/api/sources/{source_id}/analyze")
    async def analyze(source_id: str, body: AnalyzeInput):
        source = required("source", source_id)
        params = {"source_id": source_id, **body.model_dump()}
        previous = existing_request(body.request_id, "analysis", params)
        if previous:
            if previous["status"] != "completed":
                raise HTTPException(409, previous.get("error") or "분석이 진행 중입니다")
            return previous
        await acquire()
        analysis = {"id": new_id("analysis"), "source_id": source_id, "document": None, "facts": [], "usage": usage(), "warnings": [], "status": "running", "created_at": now(), "error": None, "consumed_by": None, "prompt_version": PROMPT_VERSION}
        try:
            store.put("analysis", analysis["id"], analysis)
            store.claim(body.request_id, "analysis", analysis["id"], fingerprint(params))
            if body.trace:
                analysis["usage"]["trace_id"] = await trace_start("analysis", {"provider": provider.name, "operation": "analysis"})
                if not analysis["usage"]["trace_id"]:
                    analysis["warnings"].append("LangSmith 추적 미설정 또는 연결 실패")
            raw = await call_provider(provider, "analyst", "analyze", {"document": source["document"]}, analysis["usage"], body.trace)
            doc, facts = analyze_document(source, raw)
            analysis.update(document=doc, facts=facts, warnings=[*analysis["warnings"], *doc["warnings"]], status="completed")
            source.update(document=doc, analysis=analysis)
            store.put("source", source_id, source)
            store.put("analysis", analysis["id"], analysis)
            return analysis
        except asyncio.CancelledError:
            analysis.update(status="interrupted", error="분석이 중단되었습니다. 자동 재개하지 않습니다.")
            store.put("analysis", analysis["id"], analysis)
            raise
        except Exception as exc:
            analysis.update(status="failed", error=str(exc) if isinstance(exc, ValueError) and not isinstance(exc, ValidationError) else "분석 결과 검증 또는 저장에 실패했습니다")
            store.put("analysis", analysis["id"], analysis)
            raise HTTPException(502, analysis["error"]) from None
        finally:
            try:
                analysis["warnings"].extend(analysis["usage"].get("warnings", []))
                store.put("analysis", analysis["id"], analysis)
                if body.trace:
                    if not await trace_end(analysis["usage"].get("trace_id"), {"status": analysis["status"]}):
                        analysis["warnings"].append("LangSmith 추적 종료 전송 실패 또는 미설정")
                    analysis["warnings"].extend(analysis["usage"].get("trace_warnings", []))
                    store.put("analysis", analysis["id"], analysis)
                if analysis["status"] == "completed":
                    source["analysis"] = analysis
                    store.put("source", source_id, source)
            finally:
                gate.release()

    async def execute(run):
        started = time.monotonic()
        preparation_elapsed = run["usage"]["elapsed_seconds"]
        try:
            async with asyncio.timeout(600):
                if run["trace"]:
                    run["usage"]["trace_id"] = await trace_start("job", {"provider": provider.name, "operation": "review" if run.get("mode") == "review" else "generation", "analysis_reused": bool(run["analysis_id"])})
                    if not run["usage"]["trace_id"]:
                        run["warnings"].append("LangSmith 추적 미설정 또는 연결 실패")
                else:
                    run["usage"].pop("trace_id", None)
                if run["accepted_run_id"]:
                    run["stage"] = "analyze"
                    store.put("run", run["id"], run)
                    store.event(run, "채택한 기준의 재료를 분석합니다")
                    doc = next(d for d in run["context"]["documents"] if d.get("kind") in ("draft", "revision") and d["decision"] == "accepted" and ref(d) == run["continuation_document_ref"])
                    raw = await call_provider(provider, "analyst", "analyze_continuation", {"document": doc, "context": run["context"]}, run["usage"], run["trace"])
                    # Extracted candidates are suggestions; accepted documents remain immutable.
                    from .models import AnalysisOutput
                    output = AnalysisOutput.model_validate(raw).model_dump()
                    for fact in output["facts"]:
                        fact["evidence"] = valid_evidence(fact["evidence"], doc, run["context"]["documents"])
                    run["facts"] = output["facts"]
                with tracing_context(enabled=False):
                    await build_workflow(store, provider).ainvoke({"run": run})
        except asyncio.CancelledError:
            run.update(status="interrupted", stage="interrupted", error="작업이 중단되었습니다. 자동 재개하지 않습니다.")
        except TimeoutError:
            run.update(status="failed", stage="failed", error="전체 실행 시간 600초를 초과했습니다. 자동 재개하지 않습니다.")
        except Exception as exc:
            run.update(status="failed", stage="failed", error=str(exc) if isinstance(exc, ValueError) and not isinstance(exc, ValidationError) else f"작업 실패 ({type(exc).__name__})")
        finally:
            if run["trace"]:
                if not await trace_end(run["usage"].get("trace_id"), {"status": run["status"]}):
                    run["warnings"].append("LangSmith 추적 종료 전송 실패 또는 미설정")
            run["usage"]["elapsed_seconds"] = round(preparation_elapsed + time.monotonic() - started, 3)
            run["warnings"].extend(run["usage"].get("trace_warnings", []))
            run["warnings"].extend(run["usage"].get("warnings", []))
            store.put("run", run["id"], run)
            if run["status"] in ("failed", "interrupted"):
                store.event(run, run["error"])
            else:
                store.event(run, "미해결 쟁점이 남아 있습니다" if run["status"] == "needs_attention" else "원문 검토가 끝났습니다" if run.get("mode") == "review" else "생성과 검토가 끝났습니다")
            gate.release()

    @app.post("/api/runs", status_code=202)
    async def start_run(body: RunInput):
        params = body.model_dump()
        previous = existing_request(body.request_id, "run", params)
        if previous:
            return previous
        analysis = None
        pending = []
        if body.analysis_id:
            analysis = required("analysis", body.analysis_id)
            if analysis["status"] != "completed":
                raise HTTPException(409, "완료된 분석을 선택해주세요")
            if analysis["consumed_by"]:
                # Repeated UI submission cannot charge for the same analysis twice.
                prior_run = required("run", analysis["consumed_by"])
                if prior_run.get("mode", "create") != body.mode or prior_run["constraints_input"] != body.constraints or prior_run["request_input"] != body.request or prior_run["trace"] != body.trace:
                    raise HTTPException(409, "이 분석은 이미 다른 조건의 작업에 사용되었습니다. 새로 분석해주세요.")
                store.claim(body.request_id, "run", prior_run["id"], fingerprint(params))
                return prior_run
            source_id = analysis["source_id"]
            docs = [copy.deepcopy(analysis["document"])]
            conditions = list(body.constraints)
            stats = copy.deepcopy(analysis["usage"])
        else:
            previous_run = required("run", body.accepted_run_id)
            if previous_run.get("mode", "create") == "review":
                raise HTTPException(409, "원문 검토 결과는 채택하거나 이어쓸 수 없습니다. 원문을 새로 분석해 생성해주세요")
            if previous_run["decision"]["status"] != "accepted" or previous_run["status"] not in ("completed", "needs_attention"):
                raise HTTPException(409, "채택한 최종 결과만 이어쓸 수 있습니다")
            source_id = previous_run["source_id"]
            docs = copy.deepcopy(previous_run["context"]["documents"])
            final = copy.deepcopy(previous_run["revision"] or previous_run["draft"])
            docs.append(final)
            conditions = [*previous_run["context"]["constraints"], *body.constraints]
            stats = usage()
            final_reviews = [reviewed for reviewed in previous_run["reviews"] if reviewed["document_ref"] == ref(final)]
            pending = merge_pending_issues(previous_run["context"].get("unresolved_issues", []), previous_run.get("pending_issues", []),
                                           *[issue_records(previous_run, reviewed) for reviewed in final_reviews])
        if sum(len(p["text"]) for d in docs for p in d["passages"]) + sum(len(c) for c in body.constraints) + pending_issues_length(pending) > 20000:
            raise HTTPException(422, "입력·조건·채택 설정·보존 쟁점의 기준 본문 합계를 20,000자 이하로 줄여주세요")
        await acquire()
        try:
            context = {"version": new_id("context"), "documents": docs, "constraints": conditions, "unresolved_issues": copy.deepcopy(pending)}
            # Conditions have explicit evidence passages, while remaining separately readable.
            if body.constraints:
                condition_doc = document({"title": "보호 조건", "passages": [{"id": f"c{i}", "text": c} for i,c in enumerate(body.constraints, 1)], "entities": [], "relations": []}, "input", context["version"])
                # Existing conditions are represented once, not copied as duplicate snapshots.
                context["documents"] = list(docs)
                condition_doc["origin"] = "constraint"
                context["documents"].append(condition_doc)
            default = "원문의 내부 모순과 보호 조건 충돌을 근거와 함께 검토하고 수정 의견만 제시해줘" if body.mode == "review" else "채택한 설정을 지키며 다음 사건 하나를 구체화해줘" if body.accepted_run_id else "이 재료로 세계 규칙·인물·갈등의 첫 설정 초안을 만들어줘"
            run = {"id": new_id("run"), "source_id": source_id, "analysis_id": body.analysis_id, "mode": body.mode,
                   "accepted_run_id": body.accepted_run_id, "continuation_document_ref": ref(final) if body.accepted_run_id else None, "status": "running", "stage": "queued", "request": body.request.strip() or default,
                   "request_input": body.request, "constraints_input": body.constraints, "context": context, "draft": None, "revision": None,
                   "review_document": copy.deepcopy(analysis["document"]) if body.mode == "review" else None,
                   "reviews": [], "pending_issues": copy.deepcopy(pending), "decision": {"status": "pending", "reason": ""}, "usage": stats, "error": None,
                   "warnings": ["명시적 모의 제공자: 고정된 공개 가상 예시이며 실제 AI 생성·토큰 사용 결과가 아닙니다."] if provider.name == "mock" else [],
                   "created_at": now(), "prompt_version": PROMPT_VERSION, "trace": body.trace, "provider": provider.name, "models": provider.models}
            store.put("context", context["version"], context)
            store.put("run", run["id"], run)
            store.claim(body.request_id, "run", run["id"], fingerprint(params))
            if analysis:
                analysis["consumed_by"] = run["id"]
                store.put("analysis", analysis["id"], analysis)
            store.event(run, "작업을 시작합니다")
            response = copy.deepcopy(run)
            task = asyncio.create_task(execute(run))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return response
        except Exception:
            gate.release()
            raise

    @app.get("/api/runs")
    async def list_runs():
        return {"runs": store.all("run")}

    @app.get("/api/runs/{run_id}")
    async def read_run(run_id: str):
        return required("run", run_id)

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request):
        required("run", run_id)
        try:
            after = max(0, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "Last-Event-ID는 정수여야 합니다") from None
        async def stream():
            nonlocal after
            while True:
                if await request.is_disconnected():
                    break
                for event in store.events(run_id, after):
                    after = event["id"]
                    yield f'id: {after}\nevent: progress\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
                if required("run", run_id)["status"] != "running":
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.4)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/runs/{run_id}/graph")
    async def read_graph(run_id: str, view: str = "draft"):
        if view not in ("draft", "revision", "input"):
            raise HTTPException(422, "view는 draft, revision 또는 input이어야 합니다")
        try:
            return graph(required("run", run_id), view)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/runs/{run_id}/decision")
    async def decide(run_id: str, body: DecisionInput):
        run = required("run", run_id)
        if run.get("mode", "create") == "review":
            raise HTTPException(409, "원문 검토는 수정 의견만 제공합니다. 채택·보류할 생성 문서가 없습니다")
        if run["status"] not in ("completed", "needs_attention") or not run["reviews"]:
            raise HTTPException(409, "검토가 끝난 최종 결과에만 결정할 수 있습니다")
        run["decision"] = body.model_dump()
        final = run["revision"] or run["draft"]
        final["decision"] = body.status
        store.put("run", run_id, run)
        return run

    @app.get("/api/runs/{run_id}/export")
    async def export(run_id: str):
        try:
            content = export_markdown(required("run", run_id))
            return Response(content, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'})
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    return app

app = create_app()
