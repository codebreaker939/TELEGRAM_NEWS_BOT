"""Normalized data contracts used by providers, persistence, and MCP tools."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


ContentStatus = Literal["excerpt_only", "unavailable"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonicalize_url(url: str) -> str:
    """Remove fragments while retaining the publisher's path and query parameters."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def article_id_for_url(url: str) -> str:
    digest = hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()
    return f"art_{digest[:24]}"


class RawArticle(BaseModel):
    """Provider output before validation and stable ID creation."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=500)
    source: str = Field(min_length=1, max_length=100)
    source_url: HttpUrl
    published_at: datetime
    author: str | None = Field(default=None, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2_000)
    keywords: list[str] = Field(default_factory=list)
    original_summary: str | None = Field(default=None, max_length=2_000)
    content_status: ContentStatus = "excerpt_only"

    @field_validator("published_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        return value.astimezone(UTC)


class NewsArticle(BaseModel):
    """The complete schema persisted in the archive and returned by MCP."""

    model_config = ConfigDict(str_strip_whitespace=True)

    article_id: str = Field(pattern=r"^art_[a-f0-9]{24}$")
    title: str = Field(min_length=1, max_length=500)
    source: str
    author: str | None = None
    category: str | None = None
    description: str | None = None
    source_url: HttpUrl
    published_at: datetime
    fetched_at: datetime
    keywords: list[str] = Field(default_factory=list)
    original_summary: str | None = None
    content_status: ContentStatus

    @field_validator("published_at", "fetched_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)

    @classmethod
    def from_raw(cls, raw: RawArticle, fetched_at: datetime | None = None) -> "NewsArticle":
        return cls(
            article_id=article_id_for_url(str(raw.source_url)),
            title=raw.title,
            source=raw.source,
            author=raw.author,
            category=raw.category,
            description=raw.description,
            source_url=raw.source_url,
            published_at=raw.published_at,
            fetched_at=fetched_at or utc_now(),
            keywords=raw.keywords,
            original_summary=raw.original_summary,
            content_status=raw.content_status,
        )

    def as_db_row(self) -> tuple[object, ...]:
        return (
            self.article_id,
            self.title,
            self.source,
            self.author,
            self.category,
            self.description,
            str(self.source_url),
            self.published_at.isoformat(),
            self.fetched_at.isoformat(),
            json.dumps(self.keywords),
            self.original_summary,
            self.content_status,
        )
