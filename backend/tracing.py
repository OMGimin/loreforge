"""Explicit content-free LangSmith spans; no prompts, documents, or user requests."""
import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

async def trace_start(name, metadata, parent=None):
    if not os.getenv("LANGSMITH_API_KEY"):
        return None
    id = str(uuid4())
    try:
        from langsmith import Client
        client = Client(timeout_ms=3000)
        await asyncio.wait_for(asyncio.to_thread(client.create_run, id=id, name=f"LoreForge/{name}", run_type="chain", inputs={},
            extra={"metadata": metadata}, project_name=os.getenv("LANGSMITH_PROJECT", "LoreForge"),
            # Basic /runs ingestion derives trace_id and dotted_order from parent.
            # Supplying trace_id alone makes the API reject child spans with HTTP 400.
            **({"parent_run_id": parent} if parent else {})), timeout=5)
        return id
    except Exception:
        return None

async def trace_end(id, metadata):
    if not id:
        return False
    try:
        from langsmith import Client
        await asyncio.wait_for(asyncio.to_thread(Client(timeout_ms=3000).update_run, id, outputs=metadata,
            end_time=datetime.now(timezone.utc)), timeout=5)
        return True
    except Exception:
        return False

async def trace_span(stats, name, metadata):
    root = stats.get("trace_id")
    if not root:
        return
    child = await trace_start(name, metadata, root)
    if not child or not await trace_end(child, metadata):
        stats.setdefault("trace_warnings", []).append("LangSmith 메타데이터 추적 전송 실패")
