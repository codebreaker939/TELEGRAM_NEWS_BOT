"""Authorized Telegram interface to the independent News MCP Server."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from news_bot.agent import AgentAnswer, ArticleReference, McpNewsAgent, NewsAssistantUnavailableError
from news_bot.config import BotSettings
from news_bot.digest import DailyDigestService, DigestOutcome, IST
from news_bot.mcp_client import McpNewsUnavailableError, NewsMcpClient
from news_bot.memory import DeliveryRecord, DeliveryStore, DeliveryStoreError

logger = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE_LENGTH = 4_000
ORDINALS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
    "sixth": 6,
    "6th": 6,
    "seventh": 7,
    "7th": 7,
    "eighth": 8,
    "8th": 8,
    "ninth": 9,
    "9th": 9,
    "tenth": 10,
    "10th": 10,
}
ORDINAL_REFERENCE = re.compile(
    r"\b(?:the\s+)?(" + "|".join(ORDINALS) + r")\s+(?:article|story|one)\b",
    re.IGNORECASE,
)
ARTICLE_CALLBACK = re.compile(r"^(article|why|related):(art_[0-9a-f]{24})$")


class NewsArchiveClient(Protocol):
    async def get_latest_news(self, *, limit: int) -> list[dict[str, Any]]: ...

    async def get_article(self, article_id: str) -> dict[str, Any] | None: ...

    async def refresh_news(self) -> list[dict[str, Any]]: ...


NewsClientFactory = Callable[[str], NewsArchiveClient]


class NewsAssistant(Protocol):
    async def answer(self, question: str, *, thread_id: str) -> AgentAnswer: ...


NewsAssistantFactory = Callable[[BotSettings], NewsAssistant]
DeliveryStoreFactory = Callable[[str], DeliveryStore]
DigestServiceFactory = Callable[[BotSettings, NewsArchiveClient, DeliveryStore], DailyDigestService]


class NewsBotHandlers:
    def __init__(
        self,
        settings: BotSettings,
        news_client: NewsArchiveClient,
        news_assistant: NewsAssistant,
        deliveries: DeliveryStore,
        digest_service: DailyDigestService,
    ) -> None:
        self.settings = settings
        self.news_client = news_client
        self.news_assistant = news_assistant
        self.deliveries = deliveries
        self.digest_service = digest_service

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._authorized(update):
            return
        await self._reply(
            update,
            "Welcome to the News Assistant. Use /news for the latest archived articles, "
            "/refresh to fetch current provider updates, /digest to test today's daily digest, "
            "/history to see your most recent delivery, or ask a news question.",
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
                update, "The news archive is temporarily unavailable. Please try again shortly."
            )
            return
        if not articles:
            await self._reply(
                update, "The News MCP archive has no stored articles yet. Refresh the MCP server first."
            )
            return
        for message in format_latest_news(articles):
            await self._reply(update, message)
        await self._record_delivery(update, articles, "manual_news")
        keyboard = _article_keyboard(_article_references(articles))
        if keyboard:
            await self._reply(update, "Suggested follow-ups:", reply_markup=keyboard)

    async def refresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._authorized(update):
            return
        try:
            results = await self.news_client.refresh_news()
        except McpNewsUnavailableError:
            logger.warning("Could not refresh news for an authorized Telegram request")
            await self._reply(
                update, "The news archive is temporarily unavailable. Please try again shortly."
            )
            return
        if not results:
            await self._reply(update, "The News MCP server returned no refresh results.")
            return
        await self._reply(update, format_refresh_results(results))

    async def digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        chat_id, user_id = _conversation_ids(update)
        outcome = await self.digest_service.deliver(
            chat_id=chat_id, user_id=user_id, sender=context.bot
        )
        await self._report_digest_outcome(update, outcome)

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        requested_date = context.args[0] if context.args else None
        if requested_date:
            try:
                date.fromisoformat(requested_date)
            except ValueError:
                await self._reply(update, "Use /history or /history YYYY-MM-DD.")
                return
        try:
            record = await self._delivery_for_request(update, requested_date)
        except DeliveryStoreError:
            logger.warning("Could not read delivery history", exc_info=True)
            await self._reply(update, "Your delivery history is temporarily unavailable.")
            return
        if record is None:
            date_text = f" for {requested_date}" if requested_date else ""
            await self._reply(update, f"No saved news delivery was found{date_text}.")
            return
        try:
            articles = await self._articles_for_delivery(record)
        except McpNewsUnavailableError:
            logger.warning("Could not retrieve stored delivery articles through MCP")
            await self._reply(update, "The news archive is temporarily unavailable. Please try again shortly.")
            return
        if not articles:
            await self._reply(
                update,
                "That delivery was saved, but its referenced articles are no longer available in the archive.",
            )
            return
        for message in format_articles(articles, f"News delivered on {record.delivery_date}:"):
            await self._reply(update, message)
        keyboard = _article_keyboard(_article_references(articles))
        if keyboard:
            await self._reply(update, "Suggested follow-ups:", reply_markup=keyboard)

    async def conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._authorized(update):
            return
        question = update.effective_message.text if update.effective_message else ""
        if question:
            await self._answer_question(update, await self._with_resolved_ordinal_reference(update, question))

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._authorized(update):
            return
        query = update.callback_query
        if query is None:
            return
        match = ARTICLE_CALLBACK.fullmatch(query.data or "")
        if match is None:
            await query.answer("This follow-up is no longer valid.", show_alert=True)
            return
        await query.answer()
        action, article_id = match.groups()
        prompts = {
            "article": f"Explain archived article ID {article_id} using the news archive.",
            "why": f"Why is archived article ID {article_id} important? Use the news archive.",
            "related": f"Find related archived news coverage for article ID {article_id}.",
        }
        await self._answer_question(update, prompts[action])

    async def _answer_question(self, update: Update, question: str) -> None:
        try:
            response = await self.news_assistant.answer(question, thread_id=_thread_id(update))
        except NewsAssistantUnavailableError as exc:
            logger.warning(
                "Could not answer authorized Telegram news question; root cause: %s: %s",
                type(_root_cause(exc)).__name__,
                _root_cause(exc),
            )
            await self._reply(
                update, "The news assistant is temporarily unavailable. Please try again shortly."
            )
            return
        for message in _split_messages([format_conversation_response(response)]):
            await self._reply(update, message)
        keyboard = _article_keyboard(response.article_references)
        if keyboard:
            await self._reply(update, "Suggested follow-ups:", reply_markup=keyboard)

    async def _report_digest_outcome(self, update: Update, outcome: DigestOutcome) -> None:
        if outcome.state == "sent":
            return
        messages = {
            "completed": "Today's digest was already delivered.",
            "in_progress": "Today's digest is already being prepared. Please try again shortly.",
            "empty": "No newly published archive articles are eligible for today's digest.",
            "unavailable": "The daily digest could not reach the news archive. Please try again shortly.",
            "send_failed": "The daily digest could not be sent after temporary Telegram retries.",
            "record_failed": "The digest was sent, but its delivery record needs operator attention.",
        }
        await self._reply(update, messages.get(outcome.state, "The daily digest is unavailable."))

    async def _with_resolved_ordinal_reference(self, update: Update, question: str) -> str:
        match = ORDINAL_REFERENCE.search(question)
        if match is None:
            return question
        position = ORDINALS[match.group(1).lower()]
        try:
            delivery = await self._delivery_for_request(update, None)
        except DeliveryStoreError:
            logger.warning("Could not resolve a delivery reference", exc_info=True)
            return question
        if delivery is None or len(delivery.article_ids) < position:
            return question
        return f"{question}\n\nThe referenced archived article ID is {delivery.article_ids[position - 1]}."

    async def _record_delivery(
        self, update: Update, articles: list[dict[str, Any]], delivery_kind: str
    ) -> None:
        article_ids = [str(article["article_id"]) for article in articles if article.get("article_id")]
        if not article_ids:
            logger.warning("MCP latest-news response had no article IDs; delivery history was not saved")
            return
        chat_id, user_id = _conversation_ids(update)
        try:
            await self.deliveries.record_delivery(
                chat_id=chat_id,
                user_id=user_id,
                delivery_kind=delivery_kind,
                delivery_date=datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat(),
                delivered_at=datetime.now(UTC).isoformat(),
                article_ids=article_ids,
            )
        except DeliveryStoreError:
            logger.warning("News was delivered but delivery history could not be saved", exc_info=True)
            await self._reply(update, "News was delivered, but it could not be saved to your history.")

    async def _delivery_for_request(
        self, update: Update, requested_date: str | None
    ) -> DeliveryRecord | None:
        chat_id, user_id = _conversation_ids(update)
        if requested_date:
            return await self.deliveries.delivery_for_date(
                chat_id=chat_id, user_id=user_id, delivery_date=requested_date
            )
        return await self.deliveries.latest_delivery(chat_id=chat_id, user_id=user_id)

    async def _articles_for_delivery(self, record: DeliveryRecord) -> list[dict[str, Any]]:
        articles: list[dict[str, Any]] = []
        for article_id in record.article_ids:
            article = await self.news_client.get_article(article_id)
            if article is not None:
                articles.append(article)
        return articles

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
    async def _reply(
        update: Update, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
    ) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                text, disable_web_page_preview=True, reply_markup=reply_markup
            )


def format_latest_news(articles: list[dict[str, Any]]) -> list[str]:
    return format_articles(articles, "Latest news from the persisted News MCP archive:")


def format_articles(articles: list[dict[str, Any]], heading: str) -> list[str]:
    """Build Telegram-safe messages entirely from MCP article fields."""
    entries = [heading]
    for index, article in enumerate(articles, start=1):
        title = str(article.get("title") or "Untitled article")
        source = str(article.get("source") or "Unknown source")
        published_at = str(article.get("published_at") or "Unknown publication date")
        source_url = str(article.get("source_url") or "")
        description = str(article.get("description") or "No permitted excerpt is available.")
        entries.append(
            f"{index}. {title}\nSource: {source} | Published: {published_at}\n"
            f"{description}\n{source_url}".rstrip()
        )
    return _split_messages(entries)


def format_refresh_results(results: list[dict[str, Any]]) -> str:
    """Render source-level MCP refresh results without exposing internal provider errors."""
    lines = ["News refresh completed:"]
    for result in results:
        source = str(result.get("source") or "unknown source")
        status = str(result.get("status") or "unknown")
        if status == "success":
            lines.append(
                f"• {source}: fetched {result.get('fetched_count', 0)}, "
                f"new {result.get('persisted_count', 0)}, "
                f"duplicates {result.get('duplicate_count', 0)}"
            )
        else:
            lines.append(f"• {source}: {status}; no articles added")
    return "\n".join(lines)


def format_conversation_response(response: AgentAnswer) -> str:
    """Give every natural-language answer a visible archive-versus-live coverage label."""
    coverage = (
        "Live web search via Groq (not stored in the News MCP archive)"
        if response.coverage == "web_search"
        else "Archived News MCP results"
    )
    return f"📌 Coverage: {coverage}\n\n{response.text.strip()}"


def _article_references(articles: list[dict[str, Any]]) -> tuple[ArticleReference, ...]:
    return tuple(
        ArticleReference(
            article_id=str(article["article_id"]), title=str(article.get("title") or "article")
        )
        for article in articles
        if isinstance(article.get("article_id"), str)
    )


def _article_keyboard(references: tuple[ArticleReference, ...]) -> InlineKeyboardMarkup | None:
    if not references:
        return None
    article = references[0]
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Explain this article", callback_data=f"article:{article.article_id}")],
            [InlineKeyboardButton("Why it matters", callback_data=f"why:{article.article_id}")],
            [InlineKeyboardButton("Find related coverage", callback_data=f"related:{article.article_id}")],
        ]
    )


def _conversation_ids(update: Update) -> tuple[int, int]:
    if update.effective_chat is None or update.effective_user is None:
        raise ValueError("Telegram update has no chat or user")
    return update.effective_chat.id, update.effective_user.id


def _thread_id(update: Update) -> str:
    chat_id, user_id = _conversation_ids(update)
    return f"telegram:{chat_id}:{user_id}"


def _root_cause(error: Exception) -> Exception:
    cause = error
    while isinstance(cause.__cause__, Exception):
        cause = cause.__cause__
    return cause


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
    settings: BotSettings,
    news_client_factory: NewsClientFactory = NewsMcpClient,
    news_assistant_factory: NewsAssistantFactory = McpNewsAgent,
    delivery_store_factory: DeliveryStoreFactory = DeliveryStore,
    digest_service_factory: DigestServiceFactory = DailyDigestService,
) -> Application:
    """Construct handlers without starting the independently executable bot process."""
    deliveries = delivery_store_factory(settings.database_path)
    archive = news_client_factory(settings.mcp_server_url)
    digest_service = digest_service_factory(settings, archive, deliveries)
    handlers = NewsBotHandlers(
        settings,
        archive,
        news_assistant_factory(settings),
        deliveries,
        digest_service,
    )

    async def initialize_delivery_store(_: Application) -> None:
        await deliveries.initialize()
        if application.job_queue is None:
            raise RuntimeError("Daily digest requires python-telegram-bot[job-queue]")
        for chat_id in settings.scheduled_chat_ids:
            application.job_queue.run_daily(
                scheduled_digest,
                time=time(hour=7, minute=16, tzinfo=IST),
                name=f"daily-digest:{chat_id}",
                data=chat_id,
            )

    async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = int(context.job.data)
        outcome = await digest_service.deliver(
            chat_id=chat_id, user_id=chat_id, sender=context.bot
        )
        logger.info("Scheduled digest target=%s state=%s articles=%s", chat_id, outcome.state, outcome.article_count)

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(initialize_delivery_store)
        .build()
    )
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("news", handlers.news))
    application.add_handler(CommandHandler("refresh", handlers.refresh))
    application.add_handler(CommandHandler("digest", handlers.digest))
    application.add_handler(CommandHandler("history", handlers.history))
    application.add_handler(CallbackQueryHandler(handlers.callback, pattern=r"^(article|why|related):"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.conversation))
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Telegram's Bot API places the token in the request path; never log HTTP URLs at INFO level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    application = create_application(BotSettings.from_env())
    application.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=3)
