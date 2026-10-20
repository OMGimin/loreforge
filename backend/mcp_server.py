"""Read-only MCP stdio server. No network, mutation, or model-controlled tools."""
import json
import os
import sqlite3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("LoreForge passage store")


def get_passages(context_version: str, passage_ids: list[str]) -> list[dict]:
    """Read explicit full passage keys document_id:version:passage_id in one snapshot."""
    if len(passage_ids) > 20000:
        raise ValueError("문단 조회 한도를 초과했습니다")
    # mode=ro prevents accidental creation or writes even if a future tool changes.
    path = os.path.abspath(os.getenv("LORE_DB_PATH", "runtime/loreforge.db"))
    from pathlib import Path
    db = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT value FROM objects WHERE kind='context' AND id=?", (context_version,)).fetchone()
        if row is None:
            raise ValueError("기준 스냅샷이 없습니다")
        context = json.loads(row[0])
        mapping = {f'{d["document_id"]}:{d["version"]}:{p["id"]}': {"document_ref": f'{d["document_id"]}:{d["version"]}', **p} for d in context["documents"] for p in d["passages"]}
        if any(key not in mapping for key in passage_ids):
            raise ValueError("기준에 없는 문단입니다")
        return [mapping[key] for key in passage_ids]
    finally:
        db.close()


mcp.tool()(get_passages)

if __name__ == "__main__":
    mcp.run(transport="stdio")
