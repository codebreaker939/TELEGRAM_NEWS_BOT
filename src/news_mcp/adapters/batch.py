"""DeepLearning.AI The Batch adapter with conservative, independently fail-safe transports."""

from __future__ import annotations

import asyncio
import calendar
import imaplib
import json
import re
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import Message
from email.policy import default
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from news_mcp.adapters import ProviderUnavailableError
from news_mcp.adapters.techcrunch import TechCrunchAdapter
from news_mcp.models import RawArticle

ISSUE_URL_PATTERN = re.compile(
    r"^https://www\.deeplearning\.ai/the-batch/issue-[A-Za-z0-9_-]+/?$"
)


class _JsonLdScriptExtractor(HTMLParser):
    """Collect only JSON-LD blocks that a publisher exposes as page metadata."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._inside_json_ld = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attributes = {name.lower(): value for name, value in attrs}
        self._inside_json_ld = attributes.get("type", "").lower() == "application/ld+json"
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_json_ld:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._inside_json_ld:
            self.blocks.append("".join(self._parts))
            self._inside_json_ld = False
            self._parts = []


class _BatchRssAdapter(TechCrunchAdapter):
    """RSS parsing shared with TechCrunch for an explicitly authorized Batch feed."""

    source_id = "the-batch"
    display_name = "DeepLearning.AI — The Batch"
    website_url = "https://www.deeplearning.ai/the-batch/"
    default_category = "ai-news"


class BatchAdapter:
    """Fetch only publisher metadata and verified official Batch issue URLs.

    Transport order is an explicitly configured publisher feed, publisher-provided public archive
    JSON-LD metadata, Google News RSS discovery, then an optional user subscription mailbox.
    A Google item is accepted only after its redirect resolves to the official issue URL pattern.
    """

    source_id = "the-batch"
    display_name = "DeepLearning.AI — The Batch"
    website_url = "https://www.deeplearning.ai/the-batch/"

    def __init__(
        self,
        feed_url: str | None,
        *,
        archive_url: str,
        google_news_rss_url: str,
        imap_host: str | None = None,
        imap_username: str | None = None,
        imap_password: str | None = None,
        imap_from: str | None = None,
        imap_mailbox: str = "INBOX",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._rss_adapter = _BatchRssAdapter(feed_url, client=client) if feed_url else None
        self.archive_url = archive_url
        self.google_news_rss_url = google_news_rss_url
        self.imap_host = imap_host
        self.imap_username = imap_username
        self.imap_password = imap_password
        self.imap_from = imap_from
        self.imap_mailbox = imap_mailbox
        self._client = client

    @property
    def _mailbox_configured(self) -> bool:
        return all((self.imap_host, self.imap_username, self.imap_password, self.imap_from))

    @property
    def enabled(self) -> bool:
        return bool(
            self._rss_adapter or self.archive_url or self.google_news_rss_url or self._mailbox_configured
        )

    @property
    def unavailable_reason(self) -> str | None:
        return None if self.enabled else "No The Batch acquisition method is configured"

    async def fetch(self) -> list[RawArticle]:
        if self._rss_adapter:
            return await self._rss_adapter.fetch()

        errors: list[str] = []
        for name, fetcher in (
            ("public archive metadata", self._fetch_archive_metadata),
            ("Google News RSS", self._fetch_google_news),
        ):
            try:
                articles = await fetcher()
                if articles:
                    return articles
                if name == "Google News RSS":
                    return []
                errors.append(f"{name}: no validated issue metadata")
            except ProviderUnavailableError as exc:
                errors.append(f"{name}: {exc}")

        if self._mailbox_configured:
            try:
                return await asyncio.to_thread(self._fetch_from_mailbox)
            except ProviderUnavailableError as exc:
                errors.append(f"subscription mailbox: {exc}")

        detail = "; ".join(errors) or "no acquisition method is configured"
        raise ProviderUnavailableError(f"The Batch is unavailable for this refresh: {detail}")

    async def _fetch_archive_metadata(self) -> list[RawArticle]:
        if not self.archive_url:
            raise ProviderUnavailableError("archive URL is not configured")
        content = await self._get_bytes(self.archive_url)
        articles = await asyncio.to_thread(self._parse_archive_metadata, content)
        if not articles:
            raise ProviderUnavailableError("archive did not expose valid JSON-LD issue metadata")
        return articles

    async def _fetch_google_news(self) -> list[RawArticle]:
        if not self.google_news_rss_url:
            raise ProviderUnavailableError("Google News RSS URL is not configured")
        feed_bytes = await self._get_bytes(self.google_news_rss_url)
        parsed = await asyncio.to_thread(feedparser.parse, feed_bytes)
        if (parsed.bozo and not parsed.entries) or not parsed.version:
            raise ProviderUnavailableError(
                f"Google News returned invalid RSS: "
                f"{parsed.get('bozo_exception', 'missing RSS version')}"
            )
        if not parsed.entries:
            return []

        articles: list[RawArticle] = []
        for entry in parsed.entries[:20]:
            try:
                article = await self._google_entry_to_article(entry)
            except (KeyError, TypeError, ValueError, httpx.HTTPError):
                continue
            if article is not None:
                articles.append(article)
        if not articles:
            raise ProviderUnavailableError("Google News had no verified official Batch issue URLs")
        return articles

    async def _google_entry_to_article(self, entry: Any) -> RawArticle | None:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published is None:
            return None
        source_url = await self._resolve_official_issue_url(entry.get("link"))
        if source_url is None:
            return None
        tags = [tag["term"] for tag in entry.get("tags", []) if tag.get("term")]
        excerpt = self._excerpt(entry.get("summary") or entry.get("description"))
        return RawArticle(
            title=entry["title"],
            source=self.source_id,
            source_url=source_url,
            published_at=datetime.fromtimestamp(calendar.timegm(published), tz=UTC),
            category="ai-news",
            description=excerpt,
            keywords=tags or ["AI", "newsletter"],
            original_summary=excerpt,
            content_status="excerpt_only",
        )

    async def _resolve_official_issue_url(self, url: str | None) -> str | None:
        if not url:
            return None
        current = url
        for _ in range(5):
            official_url = self._official_issue_url(current)
            if official_url:
                return official_url
            owns_client = self._client is None
            client = self._client or self._new_http_client()
            try:
                response = await client.get(current, follow_redirects=False)
            finally:
                if owns_client:
                    await client.aclose()
            if response.status_code not in {301, 302, 303, 307, 308}:
                return None
            location = response.headers.get("location")
            if not location:
                return None
            current = urljoin(current, location)
        return None

    async def _get_bytes(self, url: str) -> bytes:
        owns_client = self._client is None
        client = self._client or self._new_http_client()
        try:
            response = await client.get(url)
            if response.status_code == 429:
                raise ProviderUnavailableError("request was rate limited")
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _new_http_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={
                "User-Agent": "news-mcp-server/0.1 (+https://github.com/)",
                "Accept": "application/ld+json, application/rss+xml, application/xml, text/html;q=0.9",
            },
            follow_redirects=False,
        )

    @classmethod
    def _parse_archive_metadata(cls, content: bytes) -> list[RawArticle]:
        parser = _JsonLdScriptExtractor()
        parser.feed(content.decode("utf-8", errors="replace"))
        articles: dict[str, RawArticle] = {}
        for block in parser.blocks:
            try:
                document = json.loads(block)
            except json.JSONDecodeError:
                continue
            for node in cls._json_nodes(document):
                url = node.get("url")
                if not isinstance(url, str):
                    continue
                issue_url = cls._official_issue_url(urljoin(cls.website_url, url))
                if issue_url is None:
                    continue
                title = node.get("headline") or node.get("name")
                published_at = cls._parse_published_at(node.get("datePublished"))
                if not isinstance(title, str) or not title.strip() or published_at is None:
                    continue
                description = cls._excerpt(node.get("description"))
                articles.setdefault(
                    issue_url,
                    RawArticle(
                        title=title,
                        source=cls.source_id,
                        source_url=issue_url,
                        published_at=published_at,
                        category="ai-news",
                        description=description,
                        keywords=["AI", "newsletter"],
                        original_summary=description,
                        content_status="excerpt_only",
                    ),
                )
        return list(articles.values())

    @staticmethod
    def _json_nodes(value: object):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from BatchAdapter._json_nodes(child)
        elif isinstance(value, list):
            for child in value:
                yield from BatchAdapter._json_nodes(child)

    @staticmethod
    def _parse_published_at(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)

    @staticmethod
    def _official_issue_url(url: str) -> str | None:
        parsed = urlsplit(url)
        canonical = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return canonical if ISSUE_URL_PATTERN.fullmatch(canonical) else None

    def _fetch_from_mailbox(self) -> list[RawArticle]:
        """Read the ten newest matching emails without marking messages as read."""
        assert self.imap_host and self.imap_username and self.imap_password and self.imap_from
        try:
            with imaplib.IMAP4_SSL(self.imap_host) as client:
                status, _ = client.login(self.imap_username, self.imap_password)
                if status != "OK":
                    raise ProviderUnavailableError("The Batch IMAP login was rejected")
                status, _ = client.select(self.imap_mailbox, readonly=True)
                if status != "OK":
                    raise ProviderUnavailableError(
                        f"The Batch IMAP mailbox {self.imap_mailbox!r} could not be selected"
                    )
                status, data = client.uid("SEARCH", None, "FROM", self.imap_from)
                if status != "OK" or not data or not data[0]:
                    raise ProviderUnavailableError("No The Batch subscription messages were found")

                articles: list[RawArticle] = []
                for message_id in reversed(data[0].split()[-10:]):
                    status, message_data = client.uid("FETCH", message_id, "(RFC822)")
                    if status != "OK" or not message_data:
                        continue
                    raw_message = next(
                        (
                            item[1]
                            for item in message_data
                            if isinstance(item, tuple) and isinstance(item[1], bytes)
                        ),
                        None,
                    )
                    if raw_message is None:
                        continue
                    article = self._message_to_article(message_from_bytes(raw_message, policy=default))
                    if article is not None:
                        articles.append(article)
                if not articles:
                    raise ProviderUnavailableError(
                        "The Batch messages did not contain an official issue URL"
                    )
                return articles
        except ProviderUnavailableError:
            raise
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ProviderUnavailableError(f"The Batch IMAP fetch failed: {exc}") from exc

    def _message_to_article(self, message: Message) -> RawArticle | None:
        published_at = self._email_date(message)
        if published_at is None:
            return None
        body = self._plain_text_body(message)
        match = re.search(
            r"https://www\.deeplearning\.ai/the-batch/issue-[A-Za-z0-9_-]+/?", body
        )
        if match is None:
            return None
        title = str(message.get("Subject", "")).strip()
        if not title:
            return None
        excerpt = " ".join(unescape(body).split())[:2_000] or None
        return RawArticle(
            title=title,
            source=self.source_id,
            source_url=match.group(0),
            published_at=published_at,
            category="ai-news",
            description=excerpt,
            keywords=["AI", "newsletter"],
            original_summary=excerpt,
            content_status="excerpt_only",
        )

    @staticmethod
    def _email_date(message: Message) -> datetime | None:
        value = message.get("Date")
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else None

    @staticmethod
    def _plain_text_body(message: Message) -> str:
        if message.is_multipart():
            html_body = ""
            for part in message.walk():
                if part.get_content_disposition():
                    continue
                content = part.get_content()
                if not isinstance(content, str):
                    continue
                if part.get_content_type() == "text/plain":
                    return content
                if part.get_content_type() == "text/html":
                    html_body = content
            return BatchAdapter._html_to_text(html_body)
        content = message.get_content()
        if not isinstance(content, str):
            return ""
        return BatchAdapter._html_to_text(content) if message.get_content_type() == "text/html" else content

    @staticmethod
    def _html_to_text(value: str) -> str:
        links = re.findall(r'''href\s*=\s*["']([^"']+)["']''', value, flags=re.IGNORECASE)
        return f"{re.sub(r'<[^>]+>', ' ', value)} {' '.join(links)}"

    @staticmethod
    def _excerpt(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        plain_text = unescape(re.sub(r"<[^>]+>", " ", value))
        return " ".join(plain_text.split())[:2_000] or None
