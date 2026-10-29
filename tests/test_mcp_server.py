from __future__ import annotations

from mcp import Client

from news_mcp.config import Settings
from news_mcp.server import create_server


async def test_mcp_client_discovers_milestone_one_tools_and_resource(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "news.db",
        mit_news_rss_url="https://news.mit.edu/rss/topic/artificial-intelligence2",
        host="127.0.0.1",
        port=8000,
    )
    server = create_server(settings)

    async with Client(server) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()

    assert {tool.name for tool in tools.tools} == {
        "list_sources",
        "refresh_news",
        "get_latest_news",
    }
    assert {resource.uri for resource in resources.resources} == {"news://latest"}
