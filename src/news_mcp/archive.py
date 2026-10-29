"""Async SQLite archive for normalized news and ingestion history."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from news_mcp.models import NewsArticle


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
                    """
                )
                await db.commit()
            self._initialized = True

    async def register_source(self, source_id: str, display_name: str, website_url: str) -> None:
        await self.initialize()
        async with self._connection() as db:
            await db.execute(
                """
                INSERT INTO sources(source_id, display_name, website_url)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    website_url = excluded.website_url
                """,
                (source_id, display_name, website_url),
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
                SELECT article_id, title, source, author, category, description, source_url,
                       published_at, fetched_at, keywords_json, original_summary, content_status
                FROM articles {where}
                ORDER BY published_at DESC, article_id ASC
                LIMIT ?
                """,
                parameters,
            )
            rows = await cursor.fetchall()
        return [self._row_to_article(row) for row in rows]

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
