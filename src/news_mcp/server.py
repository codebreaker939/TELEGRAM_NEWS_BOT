"""MCP server entry point and the Phase 1 public tool surface."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from mcp.server import MCPServer

from news_mcp.adapters.batch import BatchAdapter
from news_mcp.adapters.guardian import GuardianAdapter
from news_mcp.adapters.mit_news import MITNewsAdapter
from news_mcp.adapters.techcrunch import TechCrunchAdapter
from news_mcp.archive import NewsArchive
from news_mcp.config import Settings
from news_mcp.workflow import IngestionService


def create_service(settings: Settings) -> IngestionService:
    return IngestionService(
        NewsArchive(settings.database_path),
        [
            MITNewsAdapter(settings.mit_news_rss_url),
            TechCrunchAdapter(settings.techcrunch_rss_url),
            GuardianAdapter(settings.guardian_api_key),
            BatchAdapter(
                settings.batch_rss_url,
                archive_url=settings.batch_archive_url,
                google_news_rss_url=settings.batch_google_news_rss_url,
                imap_host=settings.batch_imap_host,
                imap_username=settings.batch_imap_username,
                imap_password=settings.batch_imap_password,
                imap_from=settings.batch_imap_from,
                imap_mailbox=settings.batch_imap_mailbox,
            ),
        ],
    )


def _response(data: Any) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "schema_version": "1.0",
            "generated_at": datetime.now(UTC).isoformat(),
        },
    }


def _articles_payload(articles: list[Any]) -> list[dict[str, Any]]:
    return [article.model_dump(mode="json") for article in articles]


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
            return _response(_articles_payload(articles))
        except ValueError as exc:
            return _response({"error": str(exc)})

    @mcp.tool()
    async def search_news(
        query: str,
        source: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search persisted article metadata and permitted excerpts with SQLite FTS5."""
        try:
            articles = await news_service.search(
                query,
                source=source,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
            )
            return _response(_articles_payload(articles))
        except ValueError as exc:
            return _response({"error": str(exc)})

    @mcp.tool()
    async def get_article(article_id: str) -> dict[str, Any]:
        """Retrieve one persisted article by its stable article ID, or null when it is absent."""
        article = await news_service.get_article(article_id)
        return _response(article.model_dump(mode="json") if article else None)

    @mcp.tool()
    async def get_related_news(article_id: str) -> dict[str, Any]:
        """List potential same-story matches published by a different configured source."""
        related = await news_service.get_related_articles(article_id)
        return _response(
            [
                {
                    "article": item["article"].model_dump(mode="json"),
                    "similarity": item["similarity"],
                    "reason": item["reason"],
                }
                for item in related
            ]
        )

    @mcp.tool()
    async def get_news_by_date(date: str, source: str | None = None) -> dict[str, Any]:
        """Retrieve archived articles published on one UTC date in YYYY-MM-DD format."""
        try:
            articles = await news_service.get_by_date(date, source=source)
            return _response(_articles_payload(articles))
        except ValueError as exc:
            return _response({"error": str(exc)})

    @mcp.tool()
    async def get_digest_candidates(date: str, limit: int = 10) -> dict[str, Any]:
        """Return archived articles published on a UTC date for a future digest curator."""
        try:
            articles = await news_service.get_digest_candidates(date, limit=limit)
            return _response(_articles_payload(articles))
        except ValueError as exc:
            return _response({"error": str(exc)})

    @mcp.resource("news://latest")
    async def latest_news_resource() -> str:
        """A JSON view of the ten newest archived articles across all sources."""
        articles = await news_service.get_latest(limit=10)
        return json.dumps(_response(_articles_payload(articles)))

    @mcp.resource("news://article/{article_id}")
    async def article_resource(article_id: str) -> str:
        """A JSON view of a single article, addressed by its stable archive ID."""
        article = await news_service.get_article(article_id)
        return json.dumps(_response(article.model_dump(mode="json") if article else None))

    @mcp.resource("news://archive/{date}")
    async def archive_by_date_resource(date: str) -> str:
        """A JSON archive view for one UTC publication date in YYYY-MM-DD format."""
        try:
            articles = await news_service.get_by_date(date)
            return json.dumps(_response(_articles_payload(articles)))
        except ValueError as exc:
            return json.dumps(_response({"error": str(exc)}))

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
