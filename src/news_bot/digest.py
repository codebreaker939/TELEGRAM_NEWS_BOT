"""Deterministic, restart-aware daily digest delivery through MCP-backed news data."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut

from news_bot.config import BotSettings
from news_bot.mcp_client import McpNewsUnavailableError
from news_bot.memory import DeliveryStore, DeliveryStoreError

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
DIGEST_LOOKBACK = timedelta(hours=36)
DIGEST_LEASE = timedelta(minutes=15)
MAX_TELEGRAM_MESSAGE_LENGTH = 4_000


class DigestArchiveClient(Protocol):
    async def refresh_news(self) -> list[dict[str, Any]]: ...

    async def get_latest_news(self, *, limit: int) -> list[dict[str, Any]]: ...


class DigestSender(Protocol):
    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> object: ...


@dataclass(frozen=True, slots=True)
class DigestOutcome:
    state: str
    article_count: int = 0


class DailyDigestService:
    """Refresh, select, send, and record one recipient's daily digest."""

    def __init__(
        self,
        settings: BotSettings,
        archive: DigestArchiveClient,
        deliveries: DeliveryStore,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._archive = archive
        self._deliveries = deliveries
        self._now = now
        self._sleep = sleep

    async def deliver(self, *, chat_id: int, user_id: int, sender: DigestSender) -> DigestOutcome:
        now = self._now().astimezone(UTC)
        digest_date = now.astimezone(IST).date().isoformat()
        try:
            claim = await self._deliveries.claim_digest(
                chat_id=chat_id,
                user_id=user_id,
                digest_date=digest_date,
                now=now.isoformat(),
                lease_expires_at=(now + DIGEST_LEASE).isoformat(),
            )
        except DeliveryStoreError:
            logger.warning("Could not reserve digest delivery", exc_info=True)
            return DigestOutcome("unavailable")
        if claim.state != "claimed":
            return DigestOutcome(claim.state)

        try:
            refresh_results = await self._archive.refresh_news()
            if refresh_results and not any(item.get("status") == "success" for item in refresh_results):
                raise McpNewsUnavailableError("No news provider refresh succeeded")
            candidates = await self._archive.get_latest_news(
                limit=self._settings.digest_candidate_limit
            )
            delivered_ids = await self._deliveries.delivered_article_ids(
                chat_id=chat_id, user_id=user_id
            )
            articles = _select_digest_articles(
                candidates,
                delivered_ids=delivered_ids,
                limit=self._settings.digest_limit,
                now=now,
            )
        except (McpNewsUnavailableError, DeliveryStoreError):
            await self._release_after_failure(chat_id, user_id, digest_date, "news archive unavailable")
            return DigestOutcome("unavailable")

        if not articles:
            try:
                await self._deliveries.complete_digest(
                    chat_id=chat_id,
                    user_id=user_id,
                    digest_date=digest_date,
                    delivered_at=self._now().astimezone(UTC).isoformat(),
                    article_ids=[],
                )
            except DeliveryStoreError:
                logger.warning("Could not mark empty daily digest", exc_info=True)
                return DigestOutcome("unavailable")
            return DigestOutcome("empty")

        try:
            for message in format_daily_digest(articles, digest_date):
                await self._send_with_retries(sender, chat_id, message)
        except TelegramError:
            await self._release_after_failure(chat_id, user_id, digest_date, "Telegram send failed")
            return DigestOutcome("send_failed")

        article_ids = [str(article["article_id"]) for article in articles]
        try:
            await self._deliveries.complete_digest(
                chat_id=chat_id,
                user_id=user_id,
                digest_date=digest_date,
                delivered_at=self._now().astimezone(UTC).isoformat(),
                article_ids=article_ids,
            )
        except DeliveryStoreError:
            # The Telegram request already succeeded. Leave the lease in place to reduce duplicate sends;
            # operators must resolve this rare external-API/database commit gap before retrying manually.
            logger.exception("Digest was sent but could not be recorded")
            return DigestOutcome("record_failed", len(articles))
        return DigestOutcome("sent", len(articles))

    async def _release_after_failure(
        self, chat_id: int, user_id: int, digest_date: str, error: str
    ) -> None:
        try:
            await self._deliveries.release_digest_claim(
                chat_id=chat_id,
                user_id=user_id,
                digest_date=digest_date,
                updated_at=self._now().astimezone(UTC).isoformat(),
                error=error,
            )
        except DeliveryStoreError:
            logger.warning("Could not record failed daily digest", exc_info=True)

    async def _send_with_retries(self, sender: DigestSender, chat_id: int, message: str) -> None:
        for attempt in range(3):
            try:
                await sender.send_message(chat_id, message, disable_web_page_preview=True)
                return
            except RetryAfter as exc:
                retry_after = exc.retry_after
                delay = (
                    retry_after.total_seconds()
                    if isinstance(retry_after, timedelta)
                    else float(retry_after)
                )
            except (TimedOut, NetworkError):
                delay = float(2**attempt)
            except TelegramError:
                raise
            if attempt == 2:
                raise TelegramError("Telegram delivery exhausted its retry attempts")
            await self._sleep(delay)


def _select_digest_articles(
    candidates: list[dict[str, Any]], *, delivered_ids: set[str], limit: int, now: datetime
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    cutoff = now - DIGEST_LOOKBACK
    for article in candidates:
        article_id = article.get("article_id")
        published_at = _published_at(article.get("published_at"))
        title = str(article.get("title") or "")
        title_key = title.casefold().strip()
        if (
            not isinstance(article_id, str)
            or article_id in delivered_ids
            or published_at is None
            or published_at < cutoff
            or not title_key
            or title_key in seen_titles
        ):
            continue
        selected.append(article)
        seen_titles.add(title_key)
        if len(selected) == limit:
            break
    return selected


def _published_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def format_daily_digest(articles: list[dict[str, Any]], digest_date: str) -> list[str]:
    entries = [f"Daily news digest — {digest_date} (Asia/Kolkata)"]
    for index, article in enumerate(articles, start=1):
        title = str(article.get("title") or "Untitled article")
        source = str(article.get("source") or "Unknown source")
        published_at = str(article.get("published_at") or "Unknown publication date")
        description = str(article.get("description") or "No permitted excerpt is available.")
        source_url = str(article.get("source_url") or "")
        entries.append(
            f"{index}. {title}\n"
            f"Source: {source} | Published: {published_at}\n"
            f"{description[:350]}\n{source_url}".rstrip()
        )
    return _split_messages(entries)


def _split_messages(entries: list[str]) -> list[str]:
    messages: list[str] = []
    current = ""
    for entry in entries:
        candidate = f"{current}\n\n{entry}" if current else entry
        if len(candidate) <= MAX_TELEGRAM_MESSAGE_LENGTH:
            current = candidate
            continue
        if current:
            messages.append(current)
        while len(entry) > MAX_TELEGRAM_MESSAGE_LENGTH:
            messages.append(entry[:MAX_TELEGRAM_MESSAGE_LENGTH])
            entry = entry[MAX_TELEGRAM_MESSAGE_LENGTH:]
        current = entry
    if current:
        messages.append(current)
    return messages
