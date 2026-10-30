from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

from news_bot.agent import AgentAnswer, McpNewsAgent, NewsAssistantUnavailableError
from news_bot.app import NewsBotHandlers, format_conversation_response
from news_bot.config import BotSettings
from news_bot.digest import DailyDigestService
from news_bot.mcp_client import McpNewsUnavailableError, NewsMcpClient
from news_bot.memory import DeliveryStore


def _settings(tmp_path) -> BotSettings:
    return BotSettings(
        telegram_bot_token="test-token",
        allowed_user_ids=frozenset({42}),
        allowed_chat_ids=frozenset(),
        mcp_server_url="http://example.test/mcp",
        latest_limit=5,
        groq_model="openai/gpt-oss-20b",
        database_path=str(tmp_path / "bot.db"),
        checkpoint_database_path=str(tmp_path / "checkpoints.db"),
        conversation_message_limit=16,
        digest_limit=5,
        digest_candidate_limit=50,
        digest_chat_ids=frozenset(),
        web_search_enabled=True,
    )


def test_bot_settings_require_token_and_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("GROQ_MODEL", "")

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        BotSettings.from_env()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS"):
        BotSettings.from_env()

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42")
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        BotSettings.from_env()


def test_bot_settings_parse_authorized_ids_and_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42, 99")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-100123")
    monkeypatch.setenv("NEWS_BOT_LATEST_LIMIT", "3")
    monkeypatch.setenv("TELEGRAM_DIGEST_CHAT_IDS", "")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-20b")

    settings = BotSettings.from_env()

    assert settings.allowed_user_ids == frozenset({42, 99})
    assert settings.allowed_chat_ids == frozenset({-100123})
    assert settings.latest_limit == 3
    assert settings.groq_model == "openai/gpt-oss-20b"
    assert settings.conversation_message_limit == 16
    assert settings.scheduled_chat_ids == frozenset({42, 99, -100123})
    assert settings.web_search_enabled


class _FakeMcpClient:
    def __init__(
        self,
        server_url: str,
        result: object | None = None,
        error: Exception | None = None,
        tool_names: tuple[str, ...] = ("get_latest_news",),
    ) -> None:
        self.server_url = server_url
        self.result = result
        self.error = error
        self.tool_names = tool_names
        self.called_with: tuple[str, dict[str, int]] | None = None

    async def __aenter__(self) -> "_FakeMcpClient":
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def list_tools(self) -> object:
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self.tool_names])

    async def call_tool(self, name: str, arguments: dict[str, int]) -> object:
        self.called_with = (name, arguments)
        return self.result


@pytest.mark.asyncio
async def test_news_mcp_client_discovers_and_calls_latest_news(monkeypatch: pytest.MonkeyPatch) -> None:
    article = {"title": "Stored story", "source_url": "https://example.test/story"}
    result = SimpleNamespace(is_error=False, structured_content={"data": [article]})
    fake_client = _FakeMcpClient("http://example.test/mcp", result=result)
    monkeypatch.setattr("news_bot.mcp_client.Client", lambda url: fake_client)

    articles = await NewsMcpClient("http://example.test/mcp").get_latest_news(limit=5)

    assert articles == [article]
    assert fake_client.called_with == ("get_latest_news", {"limit": 5})


@pytest.mark.asyncio
async def test_news_mcp_client_retrieves_one_historical_article(monkeypatch: pytest.MonkeyPatch) -> None:
    article = {"article_id": "art_aaaaaaaaaaaaaaaaaaaaaaaa", "title": "Stored story"}
    result = SimpleNamespace(is_error=False, structured_content={"data": article})
    fake_client = _FakeMcpClient(
        "http://example.test/mcp", result=result, tool_names=("get_article",)
    )
    monkeypatch.setattr("news_bot.mcp_client.Client", lambda url: fake_client)

    returned_article = await NewsMcpClient("http://example.test/mcp").get_article(article["article_id"])

    assert returned_article == article
    assert fake_client.called_with == ("get_article", {"article_id": article["article_id"]})


@pytest.mark.asyncio
async def test_news_mcp_client_hides_connection_details(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeMcpClient("http://example.test/mcp", error=OSError("connection refused"))
    monkeypatch.setattr("news_bot.mcp_client.Client", lambda url: fake_client)

    with pytest.raises(McpNewsUnavailableError, match="unavailable"):
        await NewsMcpClient("http://example.test/mcp").get_latest_news(limit=5)


@tool
async def get_latest_news(limit: int) -> str:
    """Return persisted articles from the independent News MCP Server."""
    return json.dumps(
        {
            "data": [
                {
                    "article_id": "art_aaaaaaaaaaaaaaaaaaaaaaaa",
                    "title": "Stored AI story",
                    "source": "mit-news",
                    "published_at": "2026-10-30T00:00:00Z",
                    "source_url": "https://news.mit.edu/example",
                }
            ][:limit]
        }
    )


class _ToolCallingModel:
    def __init__(self) -> None:
        self.bound_tools: list[object] = []
        self.seen_messages: list[list[object]] = []

    def bind_tools(self, tools: list[object]) -> "_ToolCallingModel":
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages: list[object]) -> AIMessage:
        self.seen_messages.append(messages)
        if getattr(messages[-1], "type", "") == "tool":
            return AIMessage(
                content=(
                    "MIT published a stored AI story.\n\n"
                    "Sources:\n"
                    "- Stored AI story — MIT News, 2026-10-30T00:00:00Z — "
                    "https://news.mit.edu/example"
                )
            )
        return AIMessage(
            content="",
            tool_calls=[{"name": "get_latest_news", "args": {"limit": 1}, "id": "call-1"}],
        )


class _FakeAdapter:
    def __init__(self) -> None:
        self.entered = False
        self.requested_tools = False

    async def __aenter__(self) -> "_FakeAdapter":
        self.entered = True
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    async def list_tools(self) -> list[object]:
        self.requested_tools = True
        return [get_latest_news]


class _UnusedLatestNewsClient:
    async def get_latest_news(self, *, limit: int) -> list[dict[str, object]]:
        raise AssertionError("The conversational path must use MCPAdapter tools, not /news client")

    async def get_article(self, article_id: str) -> dict[str, object] | None:
        raise AssertionError("The conversational path must use MCPAdapter tools, not /news client")

    async def refresh_news(self) -> list[dict[str, object]]:
        raise AssertionError("The conversational path must use MCPAdapter tools, not /news client")


class _FakeTelegramMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_: object) -> None:
        self.replies.append(text)


@pytest.mark.asyncio
async def test_telegram_conversation_runs_langgraph_agent_with_mcp_tools(tmp_path) -> None:
    settings = _settings(tmp_path)
    model = _ToolCallingModel()
    adapter = _FakeAdapter()
    assistant = McpNewsAgent(
        settings,
        model_factory=lambda _: model,
        adapter_factory=lambda _: adapter,
        checkpointer=MemorySaver(),
    )
    message = _FakeTelegramMessage("What is the latest AI news?")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        effective_message=message,
    )
    deliveries = DeliveryStore(settings.database_path)
    await deliveries.initialize()
    archive = _UnusedLatestNewsClient()
    handlers = NewsBotHandlers(
        settings, archive, assistant, deliveries, DailyDigestService(settings, archive, deliveries)
    )

    await handlers.conversation(update, None)

    assert adapter.entered and adapter.requested_tools
    assert [tool.name for tool in model.bound_tools] == ["get_latest_news"]
    assert message.replies and "https://news.mit.edu/example" in message.replies[0]


class _FailingModel(_ToolCallingModel):
    async def ainvoke(self, messages: list[object]) -> AIMessage:
        raise RuntimeError("Groq rate limited the request")


class _UnavailableAdapter(_FakeAdapter):
    async def list_tools(self) -> list[object]:
        raise OSError("MCP connection refused")


class _FailingBrowserModel:
    async def ainvoke(self, messages: list[object]) -> AIMessage:
        raise RuntimeError("Groq browser search unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "adapter"),
    [(_FailingModel(), _FakeAdapter()), (_ToolCallingModel(), _UnavailableAdapter())],
)
async def test_agent_hides_model_and_mcp_failures(
    model: _ToolCallingModel, adapter: _FakeAdapter, tmp_path
) -> None:
    settings = _settings(tmp_path)
    agent = McpNewsAgent(
        settings,
        model_factory=lambda _: model,
        browser_model_factory=lambda _: _FailingBrowserModel(),
        adapter_factory=lambda _: adapter,
        checkpointer=MemorySaver(),
    )

    with pytest.raises(NewsAssistantUnavailableError, match="temporarily unavailable"):
        await agent.answer("What is the latest AI news?", thread_id="telegram:42:42")


@tool
async def search_news(query: str) -> str:
    """Search persisted archive metadata."""
    return json.dumps({"data": []})


class _ArchiveMissModel(_ToolCallingModel):
    async def ainvoke(self, messages: list[object]) -> AIMessage:
        self.seen_messages.append(messages)
        if getattr(messages[-1], "type", "") == "tool":
            return AIMessage(content="No matching archived article was found.")
        return AIMessage(
            content="",
            tool_calls=[{"name": "search_news", "args": {"query": "defense"}, "id": "call-search"}],
        )


class _WebSearchModel:
    async def ainvoke(self, messages: list[object]) -> AIMessage:
        return AIMessage(
            content=(
                "**Coverage**\nLive web search via Groq; not stored in the News MCP archive.\n\n"
                "**Answer**\nA current defense update was found.\n\n"
                "**Sources**\n- Example News — 2026-10-30 — https://example.test/defense"
            )
        )


class _UncitedArchiveModel(_ToolCallingModel):
    async def ainvoke(self, messages: list[object]) -> AIMessage:
        self.seen_messages.append(messages)
        if getattr(messages[-1], "type", "") == "tool":
            return AIMessage(content="No matching archive story was found.")
        return AIMessage(
            content="",
            tool_calls=[{"name": "get_latest_news", "args": {"limit": 1}, "id": "call-latest"}],
        )


class _SearchAdapter(_FakeAdapter):
    async def list_tools(self) -> list[object]:
        self.requested_tools = True
        return [search_news]


@pytest.mark.asyncio
async def test_empty_archive_search_uses_labeled_groq_browser_fallback(tmp_path) -> None:
    settings = _settings(tmp_path)
    agent = McpNewsAgent(
        settings,
        model_factory=lambda _: _ArchiveMissModel(),
        browser_model_factory=lambda _: _WebSearchModel(),
        adapter_factory=lambda _: _SearchAdapter(),
        checkpointer=MemorySaver(),
    )

    response = await agent.answer("What is the latest defense news?", thread_id="telegram:42:42")

    assert response.coverage == "web_search"
    assert "https://example.test/defense" in response.text
    assert "Live web search via Groq" in format_conversation_response(response)


@pytest.mark.asyncio
async def test_uncited_archive_response_uses_groq_browser_fallback(tmp_path) -> None:
    settings = _settings(tmp_path)
    agent = McpNewsAgent(
        settings,
        model_factory=lambda _: _UncitedArchiveModel(),
        browser_model_factory=lambda _: _WebSearchModel(),
        adapter_factory=lambda _: _FakeAdapter(),
        checkpointer=MemorySaver(),
    )

    response = await agent.answer("What is the latest defense news?", thread_id="telegram:42:42")

    assert response.coverage == "web_search"


@pytest.mark.asyncio
async def test_agent_uses_a_user_specific_checkpoint_thread(tmp_path) -> None:
    settings = _settings(tmp_path)
    model = _ToolCallingModel()
    agent = McpNewsAgent(
        settings,
        model_factory=lambda _: model,
        adapter_factory=lambda _: _FakeAdapter(),
        checkpointer=MemorySaver(),
    )

    await agent.answer("What is the first story?", thread_id="telegram:42:42")
    await agent.answer("Explain it further", thread_id="telegram:42:42")
    await agent.answer("A different user's question", thread_id="telegram:99:99")

    second_thread_model_input = model.seen_messages[2]
    assert any(getattr(message, "content", "") == "What is the first story?" for message in second_thread_model_input)
    isolated_thread_model_input = model.seen_messages[4]
    assert not any(getattr(message, "content", "") == "What is the first story?" for message in isolated_thread_model_input)


@pytest.mark.asyncio
async def test_delivery_store_preserves_order_and_isolates_chat_and_user(tmp_path) -> None:
    store = DeliveryStore(str(tmp_path / "deliveries.db"))
    await store.initialize()
    await store.record_delivery(
        chat_id=42,
        user_id=7,
        delivery_kind="manual_news",
        delivery_date="2026-10-30",
        delivered_at="2026-10-30T00:00:00+00:00",
        article_ids=["art_aaaaaaaaaaaaaaaaaaaaaaaa", "art_bbbbbbbbbbbbbbbbbbbbbbbb"],
    )

    record = await store.delivery_for_date(chat_id=42, user_id=7, delivery_date="2026-10-30")
    assert record is not None
    assert record.article_ids == ("art_aaaaaaaaaaaaaaaaaaaaaaaa", "art_bbbbbbbbbbbbbbbbbbbbbbbb")
    assert await store.latest_delivery(chat_id=42, user_id=8) is None
    assert await store.latest_delivery(chat_id=43, user_id=7) is None


class _ArchiveWithArticles:
    def __init__(self) -> None:
        self.articles = [
            {
                "article_id": "art_aaaaaaaaaaaaaaaaaaaaaaaa",
                "title": "First stored article",
                "source": "mit-news",
                "published_at": "2026-10-30T00:00:00Z",
                "source_url": "https://news.mit.edu/first",
                "description": "First permitted excerpt.",
            },
            {
                "article_id": "art_bbbbbbbbbbbbbbbbbbbbbbbb",
                "title": "Second stored article",
                "source": "guardian",
                "published_at": "2026-10-29T00:00:00Z",
                "source_url": "https://www.theguardian.com/second",
                "description": "Second permitted excerpt.",
            },
        ]
        self.requested_article_ids: list[str] = []
        self.refreshed = False

    async def refresh_news(self) -> list[dict[str, object]]:
        self.refreshed = True
        return [{"source": "mit-news", "status": "success"}]

    async def get_latest_news(self, *, limit: int) -> list[dict[str, object]]:
        return self.articles[:limit]

    async def get_article(self, article_id: str) -> dict[str, object] | None:
        self.requested_article_ids.append(article_id)
        return next((article for article in self.articles if article["article_id"] == article_id), None)


class _CapturingAssistant:
    def __init__(self) -> None:
        self.questions: list[tuple[str, str]] = []

    async def answer(self, question: str, *, thread_id: str):
        self.questions.append((question, thread_id))
        from news_bot.agent import AgentAnswer

        return AgentAnswer(text="Grounded response", article_references=())


@pytest.mark.asyncio
async def test_history_rereads_mcp_articles_and_ordinal_followup_uses_delivery_ids(tmp_path) -> None:
    settings = _settings(tmp_path)
    archive = _ArchiveWithArticles()
    assistant = _CapturingAssistant()
    deliveries = DeliveryStore(settings.database_path)
    await deliveries.initialize()
    handlers = NewsBotHandlers(
        settings, archive, assistant, deliveries, DailyDigestService(settings, archive, deliveries)
    )
    message = _FakeTelegramMessage("/news")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        effective_message=message,
    )

    await handlers.news(update, None)
    saved = await deliveries.latest_delivery(chat_id=42, user_id=42)
    assert saved is not None
    assert saved.article_ids == ("art_aaaaaaaaaaaaaaaaaaaaaaaa", "art_bbbbbbbbbbbbbbbbbbbbbbbb")

    await handlers.history(update, SimpleNamespace(args=[]))
    assert archive.requested_article_ids == list(saved.article_ids)

    message.text = "Explain the second article"
    await handlers.conversation(update, None)
    assert "art_bbbbbbbbbbbbbbbbbbbbbbbb" in assistant.questions[-1][0]
    assert assistant.questions[-1][1] == "telegram:42:42"


@pytest.mark.asyncio
async def test_refresh_command_uses_the_mcp_archive_client(tmp_path) -> None:
    settings = _settings(tmp_path)
    archive = _ArchiveWithArticles()
    assistant = _CapturingAssistant()
    deliveries = DeliveryStore(settings.database_path)
    await deliveries.initialize()
    handlers = NewsBotHandlers(
        settings, archive, assistant, deliveries, DailyDigestService(settings, archive, deliveries)
    )
    message = _FakeTelegramMessage("/refresh")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        effective_message=message,
    )

    await handlers.refresh(update, None)

    assert archive.refreshed
    assert "News refresh completed:" in message.replies[-1]
    assert "mit-news: fetched 0, new 0, duplicates 0" in message.replies[-1]


class _DigestSender:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_: object) -> object:
        self.messages.append((chat_id, text))
        return object()


@pytest.mark.asyncio
async def test_daily_digest_refreshes_filters_duplicates_and_prevents_restart_resend(tmp_path) -> None:
    settings = _settings(tmp_path)
    archive = _ArchiveWithArticles()
    deliveries = DeliveryStore(settings.database_path)
    await deliveries.initialize()
    await deliveries.record_delivery(
        chat_id=42,
        user_id=42,
        delivery_kind="manual_news",
        delivery_date="2026-10-29",
        delivered_at="2026-10-29T00:00:00+00:00",
        article_ids=["art_aaaaaaaaaaaaaaaaaaaaaaaa"],
    )
    sender = _DigestSender()
    now = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    service = DailyDigestService(settings, archive, deliveries, now=lambda: now)

    first = await service.deliver(chat_id=42, user_id=42, sender=sender)
    second = await service.deliver(chat_id=42, user_id=42, sender=sender)

    assert archive.refreshed
    assert first.state == "sent"
    assert first.article_count == 1
    assert second.state == "completed"
    assert len(sender.messages) == 1
    record = await deliveries.latest_delivery(chat_id=42, user_id=42)
    assert record is not None
    assert record.article_ids == ("art_bbbbbbbbbbbbbbbbbbbbbbbb",)


class _UnavailableDigestArchive(_ArchiveWithArticles):
    def __init__(self) -> None:
        super().__init__()
        self.available = False

    async def refresh_news(self) -> list[dict[str, object]]:
        if not self.available:
            raise McpNewsUnavailableError("archive unavailable")
        return await super().refresh_news()


@pytest.mark.asyncio
async def test_failed_digest_is_not_marked_as_delivered_and_can_be_retried(tmp_path) -> None:
    settings = _settings(tmp_path)
    archive = _UnavailableDigestArchive()
    deliveries = DeliveryStore(settings.database_path)
    await deliveries.initialize()
    sender = _DigestSender()
    now = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    service = DailyDigestService(settings, archive, deliveries, now=lambda: now)

    failed = await service.deliver(chat_id=42, user_id=42, sender=sender)
    archive.available = True
    retried = await service.deliver(chat_id=42, user_id=42, sender=sender)

    assert failed.state == "unavailable"
    assert retried.state == "sent"
    assert sender.messages
