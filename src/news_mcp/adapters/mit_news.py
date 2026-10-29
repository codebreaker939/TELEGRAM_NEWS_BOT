"""MIT News adapter using its public topic-specific RSS feed."""

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
USER_AGENT = "news-mcp-server/0.1 (+https://github.com/)"
RSS_ACCEPT = "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8"


class MITNewsAdapter:
    source_id = "mit-news"
    display_name = "MIT News"
    website_url = "https://news.mit.edu"
    category = "artificial-intelligence"
    enabled = True

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
                "User-Agent": USER_AGENT,
                "Accept": RSS_ACCEPT,
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
                f"MIT News returned an invalid RSS feed: "
                f"{parsed_feed.get('bozo_exception', 'missing RSS version')}"
            )

        articles: list[RawArticle] = []
        for entry in parsed_feed.entries:
            try:
                articles.append(self._normalize_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping invalid MIT News RSS entry: %s", exc)
        return articles

    async def _fetch_with_retries(self, client: httpx.AsyncClient) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                response = await client.get(self.feed_url)
                if response.status_code == 403:
                    return await self._fetch_via_curl()
                if response.status_code == 429:
                    raise ProviderUnavailableError("MIT News rate limited the RSS request")
                response.raise_for_status()
                return response.content
            except (httpx.HTTPError, ProviderUnavailableError) as exc:
                last_error = exc
                if attempt + 1 < self.retry_attempts:
                    await asyncio.sleep(0.25 * (2**attempt))
        raise ProviderUnavailableError(f"MIT News RSS fetch failed: {last_error}")

    async def _fetch_via_curl(self) -> bytes:
        """Use curl only when MIT's public RSS endpoint rejects HTTPX with 403.

        MIT serves this documented feed to curl but, in some environments, rejects
        HTTPX at its edge before feed parsing. This fallback does not fetch article
        pages or alter the declared client identity.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--connect-timeout",
                "10",
                "--max-time",
                "15",
                "--user-agent",
                USER_AGENT,
                "--header",
                f"Accept: {RSS_ACCEPT}",
                self.feed_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ProviderUnavailableError("MIT News fallback requires the curl executable") from exc

        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            details = stderr.decode("utf-8", errors="replace").strip()
            raise ProviderUnavailableError(f"MIT News curl fallback failed: {details}")
        return stdout

    def _normalize_entry(self, entry: Any) -> RawArticle:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published is None:
            raise ValueError("entry has no parseable publication date")
        published_at = datetime.fromtimestamp(calendar.timegm(published), tz=UTC)
        summary = self._excerpt(entry.get("summary") or entry.get("description"))
        tags = [tag["term"] for tag in entry.get("tags", []) if tag.get("term")]
        return RawArticle(
            title=entry["title"],
            source=self.source_id,
            source_url=entry["link"],
            published_at=published_at,
            author=entry.get("author"),
            category=self.category,
            description=summary,
            keywords=tags,
            original_summary=summary,
            content_status="excerpt_only",
        )

    @staticmethod
    def _excerpt(value: str | None) -> str | None:
        if not value:
            return None
        plain_text = unescape(re.sub(r"<[^>]+>", " ", value))
        return " ".join(plain_text.split())[:2_000] or None
