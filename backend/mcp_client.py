import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def get_passages(db_path, context_version, passage_ids):
    """Call the actual read-only MCP tool, never silently substitute a local read."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "backend.mcp_server"], env={**os.environ, "LORE_DB_PATH": str(db_path)})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = await session.call_tool("get_passages", {"context_version": context_version, "passage_ids": passage_ids})
            if result.isError:
                raise ValueError("MCP 기준 근거 조회에 실패했습니다")
            if result.structuredContent is not None:
                return result.structuredContent.get("result", result.structuredContent)
            import json
            return json.loads(result.content[0].text)
