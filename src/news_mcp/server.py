"""MCP server entry point and the Milestone 1 public tool surface."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from mcp.server import MCPServer

from news_mcp.adapters.mit_news import MITNewsAdapter
from news_mcp.archive import NewsArchive
from news_mcp.config import Settings
from news_mcp.workflow import IngestionService


def create_service(settings: Settings) -> IngestionService:
    return IngestionService(
        NewsArchive(settings.database_path),
        [MITNewsAdapter(settings.mit_news_rss_url)],
    )


def _response(data: Any) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "schema_version": "1.0",
            "generated_at": datetime.now(UTC).isoformat(),
        },
    }


def create_server(
    settings: Settings | None = None, *, service: IngestionService | None = None
) -> MCPServer:
    resolved_settings = settings or Settings.from_env()
    news_service = service or create_service(resolved_settings)
    mcp = MCPServer(
        "Independent News MCP Server",
        instructions=(
            "Use refresh_news before requesting breaking coverage. Results contain only publisher "
            "metadata and permitted feed excerpts; follow source_url for the original article."
        ),
    )

    @mcp.tool()
    async def list_sources() -> dict[str, Any]:
        """List the currently configured, independently refreshable news sources and their status."""
        return _response(await news_service.list_sources())

    @mcp.tool()
    async def refresh_news(source: str | None = None) -> dict[str, Any]:
        """Fetch, normalize, deduplicate, and persist news from one configured source or all sources."""
        try:
            return _response(await news_service.refresh(source))
        except ValueError as exc:
            return _response({"error": str(exc)})

    @mcp.tool()
    async def get_latest_news(
        source: str | None = None, category: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """Read the newest persisted articles, optionally restricted by source or category."""
        try:
            articles = await news_service.get_latest(source=source, category=category, limit=limit)
            return _response([article.model_dump(mode="json") for article in articles])
        except ValueError as exc:
            return _response({"error": str(exc)})

    @mcp.resource("news://latest")
    async def latest_news_resource() -> str:
        """A JSON view of the ten newest archived articles across all sources."""
        articles = await news_service.get_latest(limit=10)
        return json.dumps(_response([article.model_dump(mode="json") for article in articles]))

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    create_server(settings).run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        json_response=True,
    )


if __name__ == "__main__":
    main()
