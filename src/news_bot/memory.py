"""Bot-owned SQLite storage for references to delivered MCP articles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiosqlite


class DeliveryStoreError(RuntimeError):
    """The bot's delivery-reference database could not complete an operation."""


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: int
    delivery_date: str
    article_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DigestClaim:
    state: str


class DeliveryStore:
    """Persist per-chat/user article IDs without duplicating MCP-owned article data."""

    def __init__(self, database_path: str) -> None:
        self._database_path = Path(database_path)

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._database_path) as connection:
                await connection.execute("PRAGMA foreign_keys = ON")
                await connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS delivery_runs (
                        delivery_id INTEGER PRIMARY KEY,
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        delivery_kind TEXT NOT NULL,
                        delivery_date TEXT NOT NULL,
                        delivered_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_delivery_runs_lookup
                    ON delivery_runs (chat_id, user_id, delivery_date, delivery_id DESC);

                    CREATE TABLE IF NOT EXISTS delivery_articles (
                        delivery_id INTEGER NOT NULL REFERENCES delivery_runs(delivery_id) ON DELETE CASCADE,
                        article_position INTEGER NOT NULL,
                        article_id TEXT NOT NULL,
                        PRIMARY KEY (delivery_id, article_position)
                    );

                    CREATE TABLE IF NOT EXISTS scheduled_digest_runs (
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        digest_date TEXT NOT NULL,
                        status TEXT NOT NULL,
                        lease_expires_at TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        last_error TEXT,
                        PRIMARY KEY (chat_id, user_id, digest_date)
                    );
                    """
                )
                await connection.commit()
        except aiosqlite.Error as exc:
            raise DeliveryStoreError("Could not initialize delivery history") from exc

    async def record_delivery(
        self,
        *,
        chat_id: int,
        user_id: int,
        delivery_kind: str,
        delivery_date: str,
        delivered_at: str,
        article_ids: list[str],
    ) -> DeliveryRecord:
        if not article_ids:
            raise ValueError("A delivery must contain at least one article ID")
        try:
            async with aiosqlite.connect(self._database_path) as connection:
                await connection.execute("PRAGMA foreign_keys = ON")
                cursor = await connection.execute(
                    """
                    INSERT INTO delivery_runs (chat_id, user_id, delivery_kind, delivery_date, delivered_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chat_id, user_id, delivery_kind, delivery_date, delivered_at),
                )
                delivery_id = cursor.lastrowid
                assert delivery_id is not None
                await connection.executemany(
                    """
                    INSERT INTO delivery_articles (delivery_id, article_position, article_id)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (delivery_id, position, article_id)
                        for position, article_id in enumerate(article_ids, start=1)
                    ],
                )
                await connection.commit()
        except aiosqlite.Error as exc:
            raise DeliveryStoreError("Could not save delivery history") from exc
        return DeliveryRecord(delivery_id, delivery_date, tuple(article_ids))

    async def delivered_article_ids(self, *, chat_id: int, user_id: int) -> set[str]:
        try:
            async with aiosqlite.connect(self._database_path) as connection:
                async with connection.execute(
                    """
                    SELECT DISTINCT delivery_articles.article_id
                    FROM delivery_articles
                    JOIN delivery_runs USING (delivery_id)
                    WHERE delivery_runs.chat_id = ? AND delivery_runs.user_id = ?
                    """,
                    (chat_id, user_id),
                ) as cursor:
                    rows = await cursor.fetchall()
        except aiosqlite.Error as exc:
            raise DeliveryStoreError("Could not read delivery history") from exc
        return {str(row[0]) for row in rows}

    async def claim_digest(
        self,
        *,
        chat_id: int,
        user_id: int,
        digest_date: str,
        now: str,
        lease_expires_at: str,
    ) -> DigestClaim:
        """Atomically reserve one recipient's digest date across restarts."""
        try:
            async with aiosqlite.connect(self._database_path) as connection:
                await connection.execute("BEGIN IMMEDIATE")
                async with connection.execute(
                    """
                    SELECT status, lease_expires_at FROM scheduled_digest_runs
                    WHERE chat_id = ? AND user_id = ? AND digest_date = ?
                    """,
                    (chat_id, user_id, digest_date),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None and row[0] in {"sent", "empty"}:
                    await connection.commit()
                    return DigestClaim("completed")
                if row is not None and row[0] == "sending" and row[1] and row[1] > now:
                    await connection.commit()
                    return DigestClaim("in_progress")
                if row is None:
                    await connection.execute(
                        """
                        INSERT INTO scheduled_digest_runs
                        (chat_id, user_id, digest_date, status, lease_expires_at, attempts, updated_at)
                        VALUES (?, ?, ?, 'sending', ?, 1, ?)
                        """,
                        (chat_id, user_id, digest_date, lease_expires_at, now),
                    )
                else:
                    await connection.execute(
                        """
                        UPDATE scheduled_digest_runs
                        SET status = 'sending', lease_expires_at = ?, attempts = attempts + 1,
                            updated_at = ?, last_error = NULL
                        WHERE chat_id = ? AND user_id = ? AND digest_date = ?
                        """,
                        (lease_expires_at, now, chat_id, user_id, digest_date),
                    )
                await connection.commit()
        except aiosqlite.Error as exc:
            raise DeliveryStoreError("Could not reserve daily digest delivery") from exc
        return DigestClaim("claimed")

    async def complete_digest(
        self,
        *,
        chat_id: int,
        user_id: int,
        digest_date: str,
        delivered_at: str,
        article_ids: list[str],
    ) -> None:
        """Finish a claimed digest and persist its ordered MCP article references."""
        try:
            async with aiosqlite.connect(self._database_path) as connection:
                await connection.execute("PRAGMA foreign_keys = ON")
                await connection.execute("BEGIN IMMEDIATE")
                if article_ids:
                    cursor = await connection.execute(
                        """
                        INSERT INTO delivery_runs (chat_id, user_id, delivery_kind, delivery_date, delivered_at)
                        VALUES (?, ?, 'daily_digest', ?, ?)
                        """,
                        (chat_id, user_id, digest_date, delivered_at),
                    )
                    delivery_id = cursor.lastrowid
                    assert delivery_id is not None
                    await connection.executemany(
                        """
                        INSERT INTO delivery_articles (delivery_id, article_position, article_id)
                        VALUES (?, ?, ?)
                        """,
                        [
                            (delivery_id, position, article_id)
                            for position, article_id in enumerate(article_ids, start=1)
                        ],
                    )
                await connection.execute(
                    """
                    UPDATE scheduled_digest_runs
                    SET status = ?, lease_expires_at = NULL, updated_at = ?, last_error = NULL
                    WHERE chat_id = ? AND user_id = ? AND digest_date = ?
                    """,
                    (
                        "sent" if article_ids else "empty",
                        delivered_at,
                        chat_id,
                        user_id,
                        digest_date,
                    ),
                )
                await connection.commit()
        except aiosqlite.Error as exc:
            raise DeliveryStoreError("Could not finalize daily digest delivery") from exc

    async def release_digest_claim(
        self,
        *,
        chat_id: int,
        user_id: int,
        digest_date: str,
        updated_at: str,
        error: str,
    ) -> None:
        try:
            async with aiosqlite.connect(self._database_path) as connection:
                await connection.execute(
                    """
                    UPDATE scheduled_digest_runs
                    SET status = 'pending', lease_expires_at = NULL, updated_at = ?, last_error = ?
                    WHERE chat_id = ? AND user_id = ? AND digest_date = ?
                    """,
                    (updated_at, error[:500], chat_id, user_id, digest_date),
                )
                await connection.commit()
        except aiosqlite.Error as exc:
            raise DeliveryStoreError("Could not record daily digest failure") from exc

    async def latest_delivery(self, *, chat_id: int, user_id: int) -> DeliveryRecord | None:
        return await self._find_delivery(chat_id=chat_id, user_id=user_id, delivery_date=None)

    async def delivery_for_date(
        self, *, chat_id: int, user_id: int, delivery_date: str
    ) -> DeliveryRecord | None:
        return await self._find_delivery(
            chat_id=chat_id, user_id=user_id, delivery_date=delivery_date
        )

    async def _find_delivery(
        self, *, chat_id: int, user_id: int, delivery_date: str | None
    ) -> DeliveryRecord | None:
        where = "chat_id = ? AND user_id = ?"
        values: list[object] = [chat_id, user_id]
        if delivery_date is not None:
            where += " AND delivery_date = ?"
            values.append(delivery_date)
        try:
            async with aiosqlite.connect(self._database_path) as connection:
                async with connection.execute(
                    f"""
                    SELECT delivery_id, delivery_date
                    FROM delivery_runs
                    WHERE {where}
                    ORDER BY delivery_id DESC
                    LIMIT 1
                    """,
                    values,
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    return None
                async with connection.execute(
                    """
                    SELECT article_id FROM delivery_articles
                    WHERE delivery_id = ?
                    ORDER BY article_position ASC
                    """,
                    (row[0],),
                ) as cursor:
                    article_rows = await cursor.fetchall()
        except aiosqlite.Error as exc:
            raise DeliveryStoreError("Could not read delivery history") from exc
        return DeliveryRecord(
            delivery_id=int(row[0]),
            delivery_date=str(row[1]),
            article_ids=tuple(str(article_row[0]) for article_row in article_rows),
        )
