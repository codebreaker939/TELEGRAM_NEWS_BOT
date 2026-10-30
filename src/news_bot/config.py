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


@dataclass(frozen=True, slots=True)
class BotSettings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    mcp_server_url: str
    latest_limit: int
    groq_model: str

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
        return cls(
            telegram_bot_token=token,
            allowed_user_ids=allowed_user_ids,
            allowed_chat_ids=allowed_chat_ids,
            mcp_server_url=os.getenv("NEWS_BOT_MCP_URL", "http://127.0.0.1:8000/mcp"),
            latest_limit=latest_limit,
            groq_model=groq_model,
        )
