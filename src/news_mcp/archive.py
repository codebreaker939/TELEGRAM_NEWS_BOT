"""Async SQLite archive for normalized news and ingestion history."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from news_mcp.models import NewsArticle

ARTICLE_COLUMNS = """
    article_id, title, source, author, category, description, source_url,
    published_at, fetched_at, keywords_json, original_summary, content_status
"""


class NewsArchive:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            async with self._connection() as db:
                await db.executescript(
                    """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE IF NOT EXISTS sources (
                        source_id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        website_url TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        last_status TEXT,
                        last_checked_at TEXT,
                        last_error TEXT
                    );

                    CREATE TABLE IF NOT EXISTS articles (
                        article_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        source TEXT NOT NULL REFERENCES sources(source_id),
                        author TEXT,
                        category TEXT,
                        description TEXT,
                        source_url TEXT NOT NULL UNIQUE,
                        published_at TEXT NOT NULL,
                        fetched_at TEXT NOT NULL,
                        keywords_json TEXT NOT NULL,
                        original_summary TEXT,
                        content_status TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS articles_published_at_idx
                        ON articles(published_at DESC);
                    CREATE INDEX IF NOT EXISTS articles_source_published_at_idx
                        ON articles(source, published_at DESC);

                    CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts
                    USING fts5(article_id UNINDEXED, title, description, keywords, source);

                    CREATE TABLE IF NOT EXISTS ingestion_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        source TEXT NOT NULL REFERENCES sources(source_id),
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        status TEXT NOT NULL,
                        fetched_count INTEGER NOT NULL DEFAULT 0,
                        persisted_count INTEGER NOT NULL DEFAULT 0,
                        duplicate_count INTEGER NOT NULL DEFAULT 0,
                        error_message TEXT
                    );

                    CREATE TABLE IF NOT EXISTS article_duplicate_candidates (
                        article_id TEXT NOT NULL REFERENCES articles(article_id),
                        related_article_id TEXT NOT NULL REFERENCES articles(article_id),
                        similarity REAL NOT NULL,
                        reason TEXT NOT NULL,
                        detected_at TEXT NOT NULL,
                        PRIMARY KEY(article_id, related_article_id),
                        CHECK(article_id < related_article_id)
                    );
                    """
                )
                await db.commit()
            self._initialized = True

    async def register_source(
        self,
        source_id: str,
        display_name: str,
        website_url: str,
        *,
        enabled: bool = True,
        unavailable_reason: str | None = None,
    ) -> None:
        await self.initialize()
        async with self._connection() as db:
            await db.execute(
                """
                INSERT INTO sources(
                    source_id, display_name, website_url, enabled, last_status, last_error
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    website_url = excluded.website_url,
                    enabled = excluded.enabled,
                    last_status = CASE WHEN excluded.enabled = 0 THEN 'disabled' ELSE sources.last_status END,
                    last_error = CASE WHEN excluded.enabled = 0 THEN excluded.last_error ELSE sources.last_error END
                """,
                (
                    source_id,
                    display_name,
                    website_url,
                    enabled,
                    None if enabled else "disabled",
                    unavailable_reason if not enabled else None,
                ),
            )
            await db.commit()

    async def list_sources(self) -> list[dict[str, object]]:
        await self.initialize()
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT source_id, display_name, website_url, enabled, last_status,
                       last_checked_at, last_error
                FROM sources ORDER BY display_name
                """
            )
            rows = await cursor.fetchall()
        return [
            {
                "source": row[0],
                "display_name": row[1],
                "website_url": row[2],
                "enabled": bool(row[3]),
                "last_status": row[4],
                "last_checked_at": row[5],
                "last_error": row[6],
            }
            for row in rows
        ]

    async def start_attempt(self, attempt_id: str, source: str, started_at: datetime) -> None:
        await self.initialize()
        async with self._connection() as db:
            await db.execute(
                """
                INSERT INTO ingestion_attempts(attempt_id, source, started_at, status)
                VALUES (?, ?, ?, 'running')
                """,
                (attempt_id, source, started_at.isoformat()),
            )
            await db.commit()

    async def finish_attempt(
        self,
        attempt_id: str,
        source: str,
        *,
        status: str,
        fetched_count: int,
        persisted_count: int,
        duplicate_count: int,
        error_message: str | None = None,
    ) -> None:
        checked_at = datetime.now(UTC).isoformat()
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE ingestion_attempts
                SET completed_at = ?, status = ?, fetched_count = ?, persisted_count = ?,
                    duplicate_count = ?, error_message = ?
                WHERE attempt_id = ?
                """,
                (
                    checked_at,
                    status,
                    fetched_count,
                    persisted_count,
                    duplicate_count,
                    error_message,
                    attempt_id,
                ),
            )
            await db.execute(
                """
                UPDATE sources
                SET last_status = ?, last_checked_at = ?, last_error = ?
                WHERE source_id = ?
                """,
                (status, checked_at, error_message, source),
            )
            await db.commit()

    async def persist_articles(self, articles: Iterable[NewsArticle]) -> tuple[int, int]:
        """Upsert articles, returning (new_records, pre-existing_records)."""
        items = list(articles)
        if not items:
            return 0, 0
        await self.initialize()
        inserted = 0
        duplicates = 0
        async with self._connection() as db:
            for article in items:
                cursor = await db.execute(
                    "SELECT 1 FROM articles WHERE article_id = ?", (article.article_id,)
                )
                exists = await cursor.fetchone() is not None
                if exists:
                    duplicates += 1
                else:
                    inserted += 1
                await db.execute(
                    """
                    INSERT INTO articles(
                        article_id, title, source, author, category, description, source_url,
                        published_at, fetched_at, keywords_json, original_summary, content_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(article_id) DO UPDATE SET
                        title = excluded.title,
                        author = excluded.author,
                        category = excluded.category,
                        description = excluded.description,
                        source_url = excluded.source_url,
                        published_at = excluded.published_at,
                        fetched_at = excluded.fetched_at,
                        keywords_json = excluded.keywords_json,
                        original_summary = excluded.original_summary,
                        content_status = excluded.content_status
                    """,
                    article.as_db_row(),
                )
                await db.execute("DELETE FROM articles_fts WHERE article_id = ?", (article.article_id,))
                await db.execute(
                    """
                    INSERT INTO articles_fts(article_id, title, description, keywords, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        article.article_id,
                        article.title,
                        article.description or "",
                        " ".join(article.keywords),
                        article.source,
                    ),
                )
                await self._record_duplicate_candidates(db, article)
            await db.commit()
        return inserted, duplicates

    async def get_latest(
        self, *, source: str | None = None, category: str | None = None, limit: int = 10
    ) -> list[NewsArticle]:
        await self.initialize()
        filters: list[str] = []
        parameters: list[object] = []
        if source:
            filters.append("source = ?")
            parameters.append(source)
        if category:
            filters.append("category = ?")
            parameters.append(category)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        parameters.append(limit)
        async with self._connection() as db:
            cursor = await db.execute(
                f"""
                SELECT {ARTICLE_COLUMNS}
                FROM articles {where}
                ORDER BY published_at DESC, article_id ASC
                LIMIT ?
                """,
                parameters,
            )
            rows = await cursor.fetchall()
        return [self._row_to_article(row) for row in rows]

    async def search(
        self,
        query: str,
        *,
        source: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 10,
    ) -> list[NewsArticle]:
        """Search indexed article metadata without exposing article bodies."""
        await self.initialize()
        fts_query = self._fts_query(query)
        filters = ["articles_fts MATCH ?"]
        parameters: list[object] = [fts_query]
        if source:
            filters.append("a.source = ?")
            parameters.append(source)
        self._append_date_range(filters, parameters, from_date, to_date, table_alias="a")
        parameters.append(limit)
        async with self._connection() as db:
            cursor = await db.execute(
                f"""
                SELECT {self._article_columns('a')}
                FROM articles_fts
                JOIN articles AS a ON a.article_id = articles_fts.article_id
                WHERE {' AND '.join(filters)}
                ORDER BY bm25(articles_fts), a.published_at DESC, a.article_id ASC
                LIMIT ?
                """,
                parameters,
            )
            rows = await cursor.fetchall()
        return [self._row_to_article(row) for row in rows]

    async def get_article(self, article_id: str) -> NewsArticle | None:
        await self.initialize()
        async with self._connection() as db:
            cursor = await db.execute(
                f"SELECT {ARTICLE_COLUMNS} FROM articles WHERE article_id = ?", (article_id,)
            )
            row = await cursor.fetchone()
        return self._row_to_article(row) if row else None

    async def get_by_date(
        self, day: date, *, source: str | None = None, limit: int = 100
    ) -> list[NewsArticle]:
        await self.initialize()
        filters = ["substr(published_at, 1, 10) = ?"]
        parameters: list[object] = [day.isoformat()]
        if source:
            filters.append("source = ?")
            parameters.append(source)
        parameters.append(limit)
        async with self._connection() as db:
            cursor = await db.execute(
                f"""
                SELECT {ARTICLE_COLUMNS}
                FROM articles
                WHERE {' AND '.join(filters)}
                ORDER BY published_at DESC, article_id ASC
                LIMIT ?
                """,
                parameters,
            )
            rows = await cursor.fetchall()
        return [self._row_to_article(row) for row in rows]

    async def get_digest_candidates(self, day: date, *, limit: int = 10) -> list[NewsArticle]:
        """Return persisted stories published on a UTC date for a later curator."""
        return await self.get_by_date(day, limit=limit)

    async def get_related_articles(self, article_id: str) -> list[dict[str, object]]:
        """Return potential same-story matches from different publishers."""
        await self.initialize()
        async with self._connection() as db:
            cursor = await db.execute(
                f"""
                SELECT {self._article_columns('a')}, d.similarity, d.reason
                FROM article_duplicate_candidates AS d
                JOIN articles AS a ON a.article_id = CASE
                    WHEN d.article_id = ? THEN d.related_article_id
                    ELSE d.article_id
                END
                WHERE d.article_id = ? OR d.related_article_id = ?
                ORDER BY d.similarity DESC, a.published_at DESC
                """,
                (article_id, article_id, article_id),
            )
            rows = await cursor.fetchall()
        return [
            {
                "article": self._row_to_article(row),
                "similarity": row[12],
                "reason": row[13],
            }
            for row in rows
        ]

    async def list_attempts(self) -> list[dict[str, object]]:
        await self.initialize()
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT attempt_id, source, started_at, completed_at, status, fetched_count,
                       persisted_count, duplicate_count, error_message
                FROM ingestion_attempts ORDER BY started_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [
            {
                "attempt_id": row[0],
                "source": row[1],
                "started_at": row[2],
                "completed_at": row[3],
                "status": row[4],
                "fetched_count": row[5],
                "persisted_count": row[6],
                "duplicate_count": row[7],
                "error_message": row[8],
            }
            for row in rows
        ]

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        if not tokens:
            raise ValueError("query must contain at least one letter or number")
        return " AND ".join(tokens)

    @staticmethod
    def _append_date_range(
        filters: list[str],
        parameters: list[object],
        from_date: date | None,
        to_date: date | None,
        *,
        table_alias: str,
    ) -> None:
        if from_date:
            filters.append(f"{table_alias}.published_at >= ?")
            parameters.append(f"{from_date.isoformat()}T00:00:00+00:00")
        if to_date:
            filters.append(f"{table_alias}.published_at < ?")
            parameters.append(f"{(to_date + timedelta(days=1)).isoformat()}T00:00:00+00:00")

    @staticmethod
    def _article_columns(table_alias: str) -> str:
        return ", ".join(f"{table_alias}.{column.strip()}" for column in ARTICLE_COLUMNS.split(","))

    async def _record_duplicate_candidates(self, db: aiosqlite.Connection, article: NewsArticle) -> None:
        cursor = await db.execute(
            """
            SELECT article_id, title
            FROM articles
            WHERE source != ? AND article_id != ?
              AND substr(published_at, 1, 10) BETWEEN ? AND ?
            """,
            (
                article.source,
                article.article_id,
                (article.published_at.date() - timedelta(days=3)).isoformat(),
                (article.published_at.date() + timedelta(days=3)).isoformat(),
            ),
        )
        for related_id, related_title in await cursor.fetchall():
            similarity, reason = self._title_similarity(article.title, related_title)
            if similarity < 0.82:
                continue
            first_id, second_id = sorted((article.article_id, related_id))
            await db.execute(
                """
                INSERT INTO article_duplicate_candidates(
                    article_id, related_article_id, similarity, reason, detected_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(article_id, related_article_id) DO UPDATE SET
                    similarity = excluded.similarity,
                    reason = excluded.reason,
                    detected_at = excluded.detected_at
                """,
                (first_id, second_id, similarity, reason, datetime.now(UTC).isoformat()),
            )

    @staticmethod
    def _title_similarity(first_title: str, second_title: str) -> tuple[float, str]:
        first_tokens = set(re.findall(r"[^\W_]+", first_title.lower(), flags=re.UNICODE))
        second_tokens = set(re.findall(r"[^\W_]+", second_title.lower(), flags=re.UNICODE))
        if not first_tokens or not second_tokens:
            return 0.0, "insufficient title tokens"
        overlap = len(first_tokens & second_tokens) / len(first_tokens | second_tokens)
        sequence_ratio = SequenceMatcher(None, first_title.lower(), second_title.lower()).ratio()
        if overlap >= 0.82:
            return overlap, "title token overlap"
        return sequence_ratio, "title sequence similarity"

    @staticmethod
    def _row_to_article(row: aiosqlite.Row) -> NewsArticle:
        return NewsArticle(
            article_id=row[0],
            title=row[1],
            source=row[2],
            author=row[3],
            category=row[4],
            description=row[5],
            source_url=row[6],
            published_at=datetime.fromisoformat(row[7]),
            fetched_at=datetime.fromisoformat(row[8]),
            keywords=json.loads(row[9]),
            original_summary=row[10],
            content_status=row[11],
        )
