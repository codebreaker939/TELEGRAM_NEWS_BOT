from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from news_mcp.adapters.mit_news import MITNewsAdapter
from news_mcp.archive import NewsArchive
from news_mcp.workflow import IngestionService


RSS_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>MIT News</title>
<item>
  <title>New AI research</title>
  <link>https://news.mit.edu/2026/new-ai-research</link>
  <pubDate>Tue, 28 Oct 2026 12:00:00 GMT</pubDate>
  <author>MIT News Office</author>
  <category>Artificial intelligence</category>
  <description><![CDATA[<p>A permitted feed excerpt about AI.</p>]]></description>
</item>
<item>
  <title>Machine learning advance</title>
  <link>https://news.mit.edu/2026/ml-advance</link>
  <pubDate>Mon, 27 Oct 2026 12:00:00 GMT</pubDate>
  <description>Another permitted excerpt.</description>
</item>
</channel></rss>"""


def successful_transport() -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(200, content=RSS_BODY, request=request)
    )


@pytest.mark.asyncio
async def test_refresh_persists_normalized_articles_and_deduplicates(tmp_path: Path) -> None:
    async with httpx.AsyncClient(transport=successful_transport()) as client:
        service = IngestionService(
            NewsArchive(tmp_path / "news.db"),
            [MITNewsAdapter("https://news.mit.edu/rss/topic/artificial-intelligence2", client=client)],
        )

        first_refresh = await service.refresh("mit-news")
        second_refresh = await service.refresh("mit-news")
        latest = await service.get_latest(source="mit-news")

    assert first_refresh == [
        {
            "source": "mit-news",
            "status": "success",
            "fetched_count": 2,
            "persisted_count": 2,
            "duplicate_count": 0,
            "error": None,
        }
    ]
    assert second_refresh[0]["persisted_count"] == 0
    assert second_refresh[0]["duplicate_count"] == 2
    assert [article.title for article in latest] == ["New AI research", "Machine learning advance"]
    assert latest[0].article_id.startswith("art_")
    assert latest[0].description == "A permitted feed excerpt about AI."


@pytest.mark.asyncio
async def test_failed_provider_is_recorded_without_raising(tmp_path: Path) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        archive = NewsArchive(tmp_path / "news.db")
        service = IngestionService(
            archive,
            [
                MITNewsAdapter(
                    "https://news.mit.edu/rss/topic/artificial-intelligence2",
                    client=client,
                    retry_attempts=1,
                )
            ],
        )
        result = await service.refresh("mit-news")

    attempts = await archive.list_attempts()
    assert result[0]["status"] == "failed"
    assert "MIT News RSS fetch failed" in str(result[0]["error"])
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["persisted_count"] == 0


@pytest.mark.asyncio
async def test_mit_403_uses_curl_compatibility_fallback() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = MITNewsAdapter(
            "https://news.mit.edu/topic/mitartificial-intelligence2-rss.xml",
            client=client,
            retry_attempts=1,
        )
        fallback = AsyncMock(return_value=RSS_BODY)
        adapter._fetch_via_curl = fallback  # type: ignore[method-assign]
        articles = await adapter.fetch()

    fallback.assert_awaited_once()
    assert [article.title for article in articles] == ["New AI research", "Machine learning advance"]
