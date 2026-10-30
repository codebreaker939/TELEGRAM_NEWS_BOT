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
        try:
            async with Client(self.server_url) as client:
                tools = await client.list_tools()
                if "get_latest_news" not in {tool.name for tool in tools.tools}:
                    raise McpNewsUnavailableError("News MCP server does not provide get_latest_news")
                result = await client.call_tool("get_latest_news", {"limit": limit})
        except McpNewsUnavailableError:
            raise
        except Exception as exc:
            logger.warning("News MCP request failed: %s", exc)
            raise McpNewsUnavailableError("News MCP server is unavailable") from exc

        if result.is_error:
            raise McpNewsUnavailableError("News MCP server rejected the latest-news request")
        payload = result.structured_content
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise McpNewsUnavailableError("News MCP server returned an invalid latest-news response")
        return [article for article in payload["data"] if isinstance(article, dict)]
