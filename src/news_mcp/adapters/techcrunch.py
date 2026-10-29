"""TechCrunch adapter using the publisher's public RSS feed."""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any

import feedparser
import httpx

from news_mcp.adapters import ProviderUnavailableError
from news_mcp.models import RawArticle

logger = logging.getLogger(__name__)


class TechCrunchAdapter:
    source_id = "techcrunch"
    display_name = "TechCrunch"
    website_url = "https://techcrunch.com"
    enabled = True
    default_category = "technology"

    @property
    def unavailable_reason(self) -> str | None:
        return None

    def __init__(
        self,
        feed_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        retry_attempts: int = 3,
    ) -> None:
        self.feed_url = feed_url
        self._client = client
        self.retry_attempts = retry_attempts

    async def fetch(self) -> list[RawArticle]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={
                "User-Agent": "news-mcp-server/0.1 (+https://github.com/)",
                "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            },
            follow_redirects=True,
        )
        try:
            feed_bytes = await self._fetch_with_retries(client)
        finally:
            if owns_client:
                await client.aclose()

        parsed_feed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if (parsed_feed.bozo and not parsed_feed.entries) or not parsed_feed.version:
            raise ProviderUnavailableError(
                f"{self.display_name} returned an invalid RSS feed: "
                f"{parsed_feed.get('bozo_exception', 'missing RSS version')}"
            )
        articles: list[RawArticle] = []
        for entry in parsed_feed.entries:
            try:
                articles.append(self._normalize_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping invalid %s RSS entry: %s", self.display_name, exc)
        return articles

    async def _fetch_with_retries(self, client: httpx.AsyncClient) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                response = await client.get(self.feed_url)
                if response.status_code == 429:
                    raise ProviderUnavailableError(f"{self.display_name} rate limited the RSS request")
                response.raise_for_status()
                return response.content
            except (httpx.HTTPError, ProviderUnavailableError) as exc:
                last_error = exc
                if attempt + 1 < self.retry_attempts:
                    await asyncio.sleep(0.25 * (2**attempt))
        raise ProviderUnavailableError(f"{self.display_name} RSS fetch failed: {last_error}")

    def _normalize_entry(self, entry: Any) -> RawArticle:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published is None:
            raise ValueError("entry has no parseable publication date")
        tags = [tag["term"] for tag in entry.get("tags", []) if tag.get("term")]
        excerpt = self._excerpt(entry.get("summary") or entry.get("description"))
        return RawArticle(
            title=entry["title"],
            source=self.source_id,
            source_url=entry["link"],
            published_at=datetime.fromtimestamp(calendar.timegm(published), tz=UTC),
            author=entry.get("author"),
            category=tags[0].lower() if tags else self.default_category,
            description=excerpt,
            keywords=tags,
            original_summary=excerpt,
            content_status="excerpt_only",
        )

    @staticmethod
    def _excerpt(value: str | None) -> str | None:
        if not value:
            return None
        plain_text = unescape(re.sub(r"<[^>]+>", " ", value))
        return " ".join(plain_text.split())[:2_000] or None
