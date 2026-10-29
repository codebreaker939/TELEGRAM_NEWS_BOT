"""Guardian adapter using the official Open Platform Content API."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any

import httpx

from news_mcp.adapters import ProviderUnavailableError
from news_mcp.models import RawArticle

logger = logging.getLogger(__name__)
API_URL = "https://content.guardianapis.com/search"


class GuardianAdapter:
    source_id = "guardian"
    display_name = "The Guardian"
    website_url = "https://www.theguardian.com"

    def __init__(
        self, api_key: str | None, *, client: httpx.AsyncClient | None = None, retry_attempts: int = 3
    ) -> None:
        self.api_key = api_key
        self._client = client
        self.retry_attempts = retry_attempts

    @property
    def enabled(self) -> bool:
        return self.api_key is not None

    @property
    def unavailable_reason(self) -> str | None:
        return None if self.enabled else "GUARDIAN_API_KEY is not configured"

    async def fetch(self) -> list[RawArticle]:
        if not self.api_key:
            raise ProviderUnavailableError("GUARDIAN_API_KEY is not configured")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        try:
            payload = await self._fetch_with_retries(client)
        finally:
            if owns_client:
                await client.aclose()

        results = payload.get("response", {}).get("results")
        if not isinstance(results, list):
            raise ProviderUnavailableError("Guardian API returned an invalid response")
        articles: list[RawArticle] = []
        for result in results:
            try:
                articles.append(self._normalize_result(result))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping invalid Guardian API result: %s", exc)
        return articles

    async def _fetch_with_retries(self, client: httpx.AsyncClient) -> dict[str, Any]:
        last_error: Exception | None = None
        params = {
            "api-key": self.api_key,
            "section": "technology|science|business|world",
            "order-by": "newest",
            "page-size": "50",
            "show-fields": "trailText,byline",
            "show-tags": "keyword",
        }
        for attempt in range(self.retry_attempts):
            try:
                response = await client.get(API_URL, params=params)
                if response.status_code == 429:
                    raise ProviderUnavailableError("Guardian API rate limited the request")
                response.raise_for_status()
                payload = response.json()
                if payload.get("response", {}).get("status") != "ok":
                    raise ProviderUnavailableError("Guardian API did not report an ok response")
                return payload
            except (httpx.HTTPError, ProviderUnavailableError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self.retry_attempts:
                    await asyncio.sleep(1.0 * (2**attempt))
        raise ProviderUnavailableError(f"Guardian API fetch failed: {last_error}")

    def _normalize_result(self, result: dict[str, Any]) -> RawArticle:
        fields = result.get("fields") or {}
        tags = [tag["webTitle"] for tag in result.get("tags", []) if tag.get("webTitle")]
        excerpt = self._excerpt(fields.get("trailText"))
        published_at = datetime.fromisoformat(result["webPublicationDate"].replace("Z", "+00:00"))
        return RawArticle(
            title=result["webTitle"],
            source=self.source_id,
            source_url=result["webUrl"],
            published_at=published_at.astimezone(UTC),
            author=fields.get("byline") or None,
            category=result.get("sectionName", "news").lower(),
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
