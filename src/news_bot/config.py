"""Environment-backed configuration for the independent Telegram process."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _id_set(value: str | None, variable_name: str) -> frozenset[int]:
    if not value:
        return frozenset()
    try:
        return frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError(f"{variable_name} must contain comma-separated integer IDs") from exc


def _bool(value: str | None, variable_name: str, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"{variable_name} must be true or false")


@dataclass(frozen=True, slots=True)
class BotSettings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    mcp_server_url: str
    latest_limit: int
    groq_model: str
    database_path: str
    checkpoint_database_path: str
    conversation_message_limit: int
    digest_limit: int
    digest_candidate_limit: int
    digest_chat_ids: frozenset[int]
    web_search_enabled: bool

    @classmethod
    def from_env(cls) -> "BotSettings":
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN must be configured")
        allowed_user_ids = _id_set(os.getenv("TELEGRAM_ALLOWED_USER_IDS"), "TELEGRAM_ALLOWED_USER_IDS")
        allowed_chat_ids = _id_set(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS"), "TELEGRAM_ALLOWED_CHAT_IDS")
        if not allowed_user_ids and not allowed_chat_ids:
            raise ValueError(
                "Configure TELEGRAM_ALLOWED_USER_IDS and/or TELEGRAM_ALLOWED_CHAT_IDS"
            )
        if not os.getenv("GROQ_API_KEY"):
            raise ValueError("GROQ_API_KEY must be configured")
        groq_model = os.getenv("GROQ_MODEL")
        if not groq_model:
            raise ValueError("GROQ_MODEL must be configured")
        latest_limit = int(os.getenv("NEWS_BOT_LATEST_LIMIT", "5"))
        if not 1 <= latest_limit <= 10:
            raise ValueError("NEWS_BOT_LATEST_LIMIT must be between 1 and 10")
        conversation_message_limit = int(os.getenv("NEWS_BOT_CONVERSATION_MESSAGE_LIMIT", "16"))
        if not 4 <= conversation_message_limit <= 40:
            raise ValueError("NEWS_BOT_CONVERSATION_MESSAGE_LIMIT must be between 4 and 40")
        digest_limit = int(os.getenv("NEWS_BOT_DIGEST_LIMIT", "5"))
        if not 1 <= digest_limit <= 10:
            raise ValueError("NEWS_BOT_DIGEST_LIMIT must be between 1 and 10")
        digest_candidate_limit = int(os.getenv("NEWS_BOT_DIGEST_CANDIDATE_LIMIT", "50"))
        if not digest_limit <= digest_candidate_limit <= 100:
            raise ValueError(
                "NEWS_BOT_DIGEST_CANDIDATE_LIMIT must be between NEWS_BOT_DIGEST_LIMIT and 100"
            )
        digest_chat_ids = _id_set(os.getenv("TELEGRAM_DIGEST_CHAT_IDS"), "TELEGRAM_DIGEST_CHAT_IDS")
        return cls(
            telegram_bot_token=token,
            allowed_user_ids=allowed_user_ids,
            allowed_chat_ids=allowed_chat_ids,
            mcp_server_url=os.getenv("NEWS_BOT_MCP_URL", "http://127.0.0.1:8000/mcp"),
            latest_limit=latest_limit,
            groq_model=groq_model,
            database_path=os.getenv("NEWS_BOT_DATABASE_PATH", "data/news_bot.db"),
            checkpoint_database_path=os.getenv(
                "NEWS_BOT_CHECKPOINT_DATABASE_PATH", "data/news_bot_checkpoints.db"
            ),
            conversation_message_limit=conversation_message_limit,
            digest_limit=digest_limit,
            digest_candidate_limit=digest_candidate_limit,
            digest_chat_ids=digest_chat_ids,
            web_search_enabled=_bool(os.getenv("NEWS_BOT_WEB_SEARCH_ENABLED"), "NEWS_BOT_WEB_SEARCH_ENABLED", True),
        )

    @property
    def scheduled_chat_ids(self) -> frozenset[int]:
        """Explicit digest targets, or the configured authorized recipient IDs."""
        return self.digest_chat_ids or (self.allowed_user_ids | self.allowed_chat_ids)
