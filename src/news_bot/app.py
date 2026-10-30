"""Minimal authorized Telegram interface to the independent News MCP Server."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from news_bot.config import BotSettings
from news_bot.mcp_client import McpNewsUnavailableError, NewsMcpClient

logger = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE_LENGTH = 4_000


class LatestNewsClient(Protocol):
    async def get_latest_news(self, *, limit: int) -> list[dict[str, Any]]: ...


NewsClientFactory = Callable[[str], LatestNewsClient]


class NewsBotHandlers:
    def __init__(self, settings: BotSettings, news_client: LatestNewsClient) -> None:
        self.settings = settings
        self.news_client = news_client

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._authorized(update):
            return
        await self._reply(
            update,
            "Welcome to the News Assistant. Use /news to read the latest articles stored by the "
            "independent News MCP Server.",
        )

    async def news(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._authorized(update):
            return
        try:
            articles = await self.news_client.get_latest_news(limit=self.settings.latest_limit)
        except McpNewsUnavailableError:
            logger.warning("Could not retrieve latest news for an authorized Telegram request")
            await self._reply(
                update,
                "The news archive is temporarily unavailable. Please try again shortly.",
            )
            return

        if not articles:
            await self._reply(
                update,
                "The News MCP archive has no stored articles yet. Refresh the MCP server first.",
            )
            return
        for message in format_latest_news(articles):
            await self._reply(update, message)

    async def _authorized(self, update: Update) -> bool:
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_allowed = not self.settings.allowed_user_ids or user_id in self.settings.allowed_user_ids
        chat_allowed = not self.settings.allowed_chat_ids or chat_id in self.settings.allowed_chat_ids
        if user_allowed and chat_allowed:
            return True
        await self._reply(update, "You are not authorized to use this bot.")
        return False

    @staticmethod
    async def _reply(update: Update, text: str) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(text, disable_web_page_preview=True)


def format_latest_news(articles: list[dict[str, Any]]) -> list[str]:
    """Build Telegram-safe messages entirely from article fields returned by MCP."""
    entries = ["Latest news from the persisted News MCP archive:"]
    for index, article in enumerate(articles, start=1):
        title = str(article.get("title") or "Untitled article")
        source = str(article.get("source") or "Unknown source")
        published_at = str(article.get("published_at") or "Unknown publication date")
        source_url = str(article.get("source_url") or "")
        description = str(article.get("description") or "No permitted excerpt is available.")
        entries.append(
            f"{index}. {title}\n"
            f"Source: {source} | Published: {published_at}\n"
            f"{description}\n"
            f"{source_url}".rstrip()
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


def create_application(
    settings: BotSettings, news_client_factory: NewsClientFactory = NewsMcpClient
) -> Application:
    """Construct handlers without starting the Telegram polling loop."""
    handlers = NewsBotHandlers(settings, news_client_factory(settings.mcp_server_url))
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("news", handlers.news))
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Telegram's Bot API places the token in the request path; never log HTTP URLs at INFO level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    application = create_application(BotSettings.from_env())
    # The library defaults to zero bootstrap retries. A brief Telegram API outage
    # should not immediately terminate the independently running bot process.
    application.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=3)
