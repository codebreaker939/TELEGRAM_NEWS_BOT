from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from news_bot.agent import McpNewsAgent, NewsAssistantUnavailableError
from news_bot.app import NewsBotHandlers
from news_bot.config import BotSettings
from news_bot.mcp_client import McpNewsUnavailableError, NewsMcpClient


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
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-20b")

    settings = BotSettings.from_env()

    assert settings.allowed_user_ids == frozenset({42, 99})
    assert settings.allowed_chat_ids == frozenset({-100123})
    assert settings.latest_limit == 3
    assert settings.groq_model == "openai/gpt-oss-20b"


class _FakeMcpClient:
    def __init__(self, server_url: str, result: object | None = None, error: Exception | None = None) -> None:
        self.server_url = server_url
        self.result = result
        self.error = error
        self.called_with: tuple[str, dict[str, int]] | None = None

    async def __aenter__(self) -> "_FakeMcpClient":
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def list_tools(self) -> object:
        return SimpleNamespace(tools=[SimpleNamespace(name="get_latest_news")])

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

    def bind_tools(self, tools: list[object]) -> "_ToolCallingModel":
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages: list[object]) -> AIMessage:
        if any(getattr(message, "type", "") == "tool" for message in messages):
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


class _FakeTelegramMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_: object) -> None:
        self.replies.append(text)


@pytest.mark.asyncio
async def test_telegram_conversation_runs_langgraph_agent_with_mcp_tools() -> None:
    settings = BotSettings(
        telegram_bot_token="test-token",
        allowed_user_ids=frozenset({42}),
        allowed_chat_ids=frozenset(),
        mcp_server_url="http://example.test/mcp",
        latest_limit=5,
        groq_model="openai/gpt-oss-20b",
    )
    model = _ToolCallingModel()
    adapter = _FakeAdapter()
    assistant = McpNewsAgent(
        settings,
        model_factory=lambda _: model,
        adapter_factory=lambda _: adapter,
    )
    message = _FakeTelegramMessage("What is the latest AI news?")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        effective_message=message,
    )
    handlers = NewsBotHandlers(settings, _UnusedLatestNewsClient(), assistant)

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "adapter"),
    [(_FailingModel(), _FakeAdapter()), (_ToolCallingModel(), _UnavailableAdapter())],
)
async def test_agent_hides_model_and_mcp_failures(
    model: _ToolCallingModel, adapter: _FakeAdapter
) -> None:
    settings = BotSettings(
        telegram_bot_token="test-token",
        allowed_user_ids=frozenset({42}),
        allowed_chat_ids=frozenset(),
        mcp_server_url="http://example.test/mcp",
        latest_limit=5,
        groq_model="openai/gpt-oss-20b",
    )
    agent = McpNewsAgent(
        settings,
        model_factory=lambda _: model,
        adapter_factory=lambda _: adapter,
    )

    with pytest.raises(NewsAssistantUnavailableError, match="temporarily unavailable"):
        await agent.answer("What is the latest AI news?")
