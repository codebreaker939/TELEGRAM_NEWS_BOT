from __future__ import annotations

import json
from datetime import UTC, datetime

from mcp import Client

from news_mcp.config import Settings
from news_mcp.models import NewsArticle, RawArticle
from news_mcp.server import create_server, create_service


async def test_mcp_client_discovers_milestone_one_tools_and_resource(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "news.db",
        mit_news_rss_url="https://news.mit.edu/rss/topic/artificial-intelligence2",
        techcrunch_rss_url="https://techcrunch.com/feed/",
        guardian_api_key=None,
        batch_rss_url=None,
        batch_archive_url="https://www.deeplearning.ai/the-batch",
        batch_google_news_rss_url="https://news.google.com/rss/search?q=the-batch",
        batch_imap_host=None,
        batch_imap_username=None,
        batch_imap_password=None,
        batch_imap_from=None,
        batch_imap_mailbox="INBOX",
        host="127.0.0.1",
        port=8000,
    )
    service = create_service(settings)
    await service.initialize()
    stored_article = NewsArticle.from_raw(
        RawArticle(
            title="Historical AI archive article",
            source="mit-news",
            source_url="https://news.mit.edu/2026/historical-ai-article",
            published_at=datetime(2026, 10, 28, tzinfo=UTC),
            category="artificial-intelligence",
            description="A permitted historical excerpt.",
            keywords=["Artificial intelligence"],
            original_summary="A permitted historical excerpt.",
        ),
        fetched_at=datetime(2026, 10, 29, tzinfo=UTC),
    )
    await service.archive.persist_articles([stored_article])
    server = create_server(settings, service=service)

    async with Client(server) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        missing_article = await client.read_resource("news://article/art_000000000000000000000000")
        latest_resource = await client.read_resource("news://latest")
        article_resource = await client.read_resource(f"news://article/{stored_article.article_id}")
        archive_resource = await client.read_resource("news://archive/2026-10-28")

    assert {tool.name for tool in tools.tools} == {
        "list_sources",
        "refresh_news",
        "get_latest_news",
        "search_news",
        "get_article",
        "get_related_news",
        "get_news_by_date",
        "get_digest_candidates",
    }
    assert {resource.uri for resource in resources.resources} == {"news://latest"}
    assert {template.uri_template for template in templates.resource_templates} == {
        "news://article/{article_id}",
        "news://archive/{date}",
    }
    assert json.loads(missing_article.contents[0].text)["data"] is None
    assert json.loads(latest_resource.contents[0].text)["data"][0]["article_id"] == stored_article.article_id
    assert json.loads(article_resource.contents[0].text)["data"]["title"] == stored_article.title
    assert json.loads(archive_resource.contents[0].text)["data"] == [stored_article.model_dump(mode="json")]


async def test_all_phase_one_sources_are_registered_with_configuration_status(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "news.db",
        mit_news_rss_url="https://news.mit.edu/topic/mitartificial-intelligence2-rss.xml",
        techcrunch_rss_url="https://techcrunch.com/feed/",
        guardian_api_key=None,
        batch_rss_url=None,
        batch_archive_url="https://www.deeplearning.ai/the-batch",
        batch_google_news_rss_url="https://news.google.com/rss/search?q=the-batch",
        batch_imap_host=None,
        batch_imap_username=None,
        batch_imap_password=None,
        batch_imap_from=None,
        batch_imap_mailbox="INBOX",
        host="127.0.0.1",
        port=8000,
    )

    sources = await create_service(settings).list_sources()
    by_id = {source["source"]: source for source in sources}

    assert set(by_id) == {"mit-news", "techcrunch", "guardian", "the-batch"}
    assert by_id["mit-news"]["enabled"]
    assert by_id["techcrunch"]["enabled"]
    assert not by_id["guardian"]["enabled"]
    assert "GUARDIAN_API_KEY" in str(by_id["guardian"]["last_error"])
    assert by_id["the-batch"]["enabled"]
