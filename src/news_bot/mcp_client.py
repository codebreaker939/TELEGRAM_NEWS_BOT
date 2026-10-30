"""Protocol-only client for the independently deployed News MCP Server."""

from __future__ import annotations

import logging
from typing import Any

from mcp import Client

logger = logging.getLogger(__name__)


class McpNewsUnavailableError(RuntimeError):
    """The MCP news archive cannot be used for the current bot request."""


class NewsMcpClient:
    """Retrieve persisted news only through the MCP Streamable HTTP interface."""

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url

    async def get_latest_news(self, *, limit: int) -> list[dict[str, Any]]:
        payload = await self._call_tool("get_latest_news", {"limit": limit})
        if not isinstance(payload.get("data"), list):
            raise McpNewsUnavailableError("News MCP server returned an invalid latest-news response")
        return [article for article in payload["data"] if isinstance(article, dict)]

    async def get_article(self, article_id: str) -> dict[str, Any] | None:
        payload = await self._call_tool("get_article", {"article_id": article_id})
        article = payload.get("data")
        if article is None:
            return None
        if not isinstance(article, dict):
            raise McpNewsUnavailableError("News MCP server returned an invalid article response")
        return article

    async def refresh_news(self) -> list[dict[str, Any]]:
        payload = await self._call_tool("refresh_news", {})
        if not isinstance(payload.get("data"), list):
            raise McpNewsUnavailableError("News MCP server returned an invalid refresh response")
        return [result for result in payload["data"] if isinstance(result, dict)]

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            async with Client(self.server_url) as client:
                tools = await client.list_tools()
                if tool_name not in {tool.name for tool in tools.tools}:
                    raise McpNewsUnavailableError(f"News MCP server does not provide {tool_name}")
                result = await client.call_tool(tool_name, arguments)
        except McpNewsUnavailableError:
            raise
        except Exception as exc:
            logger.warning("News MCP request failed: %s", exc)
            raise McpNewsUnavailableError("News MCP server is unavailable") from exc

        if result.is_error:
            raise McpNewsUnavailableError("News MCP server rejected the request")
        payload = result.structured_content
        if not isinstance(payload, dict) or "data" not in payload:
            raise McpNewsUnavailableError("News MCP server returned an invalid response")
        return payload
