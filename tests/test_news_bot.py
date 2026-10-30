from __future__ import annotations

from types import SimpleNamespace

import pytest

from news_bot.config import BotSettings
from news_bot.mcp_client import McpNewsUnavailableError, NewsMcpClient


def test_bot_settings_require_token_and_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "")

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        BotSettings.from_env()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS"):
        BotSettings.from_env()


def test_bot_settings_parse_authorized_ids_and_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42, 99")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-100123")
    monkeypatch.setenv("NEWS_BOT_LATEST_LIMIT", "3")

    settings = BotSettings.from_env()

    assert settings.allowed_user_ids == frozenset({42, 99})
    assert settings.allowed_chat_ids == frozenset({-100123})
    assert settings.latest_limit == 3


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
