from __future__ import annotations

from email.message import EmailMessage

import httpx
import pytest

from news_mcp.adapters import ProviderUnavailableError
from news_mcp.adapters.batch import BatchAdapter
from news_mcp.adapters.guardian import GuardianAdapter
from news_mcp.adapters.techcrunch import TechCrunchAdapter
from news_mcp.archive import NewsArchive
from news_mcp.workflow import IngestionService


TECHCRUNCH_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>AI startup raises funding</title><link>https://techcrunch.com/2026/ai-startup</link>
<pubDate>Wed, 29 Oct 2026 10:00:00 GMT</pubDate><category>AI</category>
<description><![CDATA[<p>A permitted RSS excerpt.</p>]]></description></item>
</channel></rss>"""

GUARDIAN_RESPONSE = {
    "response": {
        "status": "ok",
        "results": [
            {
                "webTitle": "AI policy update",
                "webUrl": "https://www.theguardian.com/technology/2026/oct/29/ai-policy",
                "webPublicationDate": "2026-10-29T09:30:00Z",
                "sectionName": "Technology",
                "fields": {"trailText": "<p>A permitted API excerpt.</p>", "byline": "Guardian Staff"},
                "tags": [{"webTitle": "Artificial intelligence"}],
            }
        ],
    }
}

BATCH_ARCHIVE_URL = "https://www.deeplearning.ai/the-batch"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q=the-batch"
GOOGLE_NEWS_ITEM_URL = "https://news.google.com/rss/articles/example"
OFFICIAL_BATCH_ISSUE_URL = "https://www.deeplearning.ai/the-batch/issue-336/"
ARCHIVE_METADATA = b"""<html><script type="application/ld+json">
{"@graph":[{"@type":"Article","url":"https://www.deeplearning.ai/the-batch/issue-336/",
"headline":"The Batch: verified archive issue","datePublished":"2026-10-30T09:00:00Z",
"description":"Publisher-provided archive metadata."}]}</script></html>"""
GOOGLE_NEWS_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><item>
<title>The Batch: Google News discovery</title><link>https://news.google.com/rss/articles/example</link>
<pubDate>Fri, 30 Oct 2026 09:00:00 GMT</pubDate><description>Google News result.</description>
</item></channel></rss>"""


def batch_adapter(**kwargs: object) -> BatchAdapter:
    return BatchAdapter(
        None,
        archive_url=BATCH_ARCHIVE_URL,
        google_news_rss_url=GOOGLE_NEWS_RSS_URL,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_techcrunch_adapter_normalizes_public_rss() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=TECHCRUNCH_RSS, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        articles = await TechCrunchAdapter("https://techcrunch.com/feed/", client=client).fetch()

    assert len(articles) == 1
    assert articles[0].source == "techcrunch"
    assert articles[0].category == "ai"
    assert articles[0].description == "A permitted RSS excerpt."


@pytest.mark.asyncio
async def test_guardian_adapter_uses_official_api_response() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=GUARDIAN_RESPONSE, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        articles = await GuardianAdapter("test-key", client=client).fetch()

    assert len(articles) == 1
    assert articles[0].source == "guardian"
    assert articles[0].author == "Guardian Staff"
    assert articles[0].description == "A permitted API excerpt."


@pytest.mark.asyncio
async def test_guardian_rate_limit_is_reported_as_provider_unavailable() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(429, request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = GuardianAdapter("test-key", client=client, retry_attempts=1)
        with pytest.raises(ProviderUnavailableError, match="rate limited"):
            await adapter.fetch()


@pytest.mark.asyncio
async def test_techcrunch_timeout_is_reported_as_provider_unavailable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        adapter = TechCrunchAdapter("https://techcrunch.com/feed/", client=client, retry_attempts=1)
        with pytest.raises(ProviderUnavailableError, match="RSS fetch failed"):
            await adapter.fetch()


@pytest.mark.asyncio
async def test_techcrunch_invalid_response_is_reported_as_provider_unavailable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"<html>not an RSS feed</html>", request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = TechCrunchAdapter("https://techcrunch.com/feed/", client=client)
        with pytest.raises(ProviderUnavailableError, match="invalid RSS feed"):
            await adapter.fetch()


def test_batch_public_archive_and_google_discovery_are_enabled_by_default() -> None:
    adapter = batch_adapter()

    assert adapter.enabled
    assert adapter.unavailable_reason is None


@pytest.mark.asyncio
async def test_batch_uses_public_archive_metadata_before_google_news() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BATCH_ARCHIVE_URL:
            return httpx.Response(200, content=ARCHIVE_METADATA, request=request)
        pytest.fail(f"Google fallback must not run after valid archive metadata: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        articles = await batch_adapter(client=client).fetch()

    assert len(articles) == 1
    assert articles[0].title == "The Batch: verified archive issue"
    assert str(articles[0].source_url) == OFFICIAL_BATCH_ISSUE_URL


@pytest.mark.asyncio
async def test_batch_uses_google_news_only_for_verified_official_issue_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BATCH_ARCHIVE_URL:
            return httpx.Response(403, request=request)
        if str(request.url) == GOOGLE_NEWS_RSS_URL:
            return httpx.Response(200, content=GOOGLE_NEWS_RSS, request=request)
        if str(request.url) == GOOGLE_NEWS_ITEM_URL:
            return httpx.Response(302, headers={"location": OFFICIAL_BATCH_ISSUE_URL}, request=request)
        pytest.fail(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        articles = await batch_adapter(client=client).fetch()

    assert len(articles) == 1
    assert str(articles[0].source_url) == OFFICIAL_BATCH_ISSUE_URL


@pytest.mark.asyncio
async def test_batch_reports_no_new_publication_for_an_empty_google_news_feed(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BATCH_ARCHIVE_URL:
            return httpx.Response(403, request=request)
        if str(request.url) == GOOGLE_NEWS_RSS_URL:
            return httpx.Response(200, content=b"<rss><channel></channel></rss>", request=request)
        pytest.fail(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = batch_adapter(client=client)
        service = IngestionService(NewsArchive(tmp_path / "news.db"), [adapter])
        result = await service.refresh("the-batch")

    assert result[0]["status"] == "success"
    assert result[0]["fetched_count"] == 0
    assert result[0]["persisted_count"] == 0
    assert result[0]["duplicate_count"] == 0
    assert result[0]["error"] is None


@pytest.mark.asyncio
async def test_batch_marks_the_source_failed_when_public_transports_are_unavailable(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = IngestionService(NewsArchive(tmp_path / "news.db"), [batch_adapter(client=client)])
        result = await service.refresh("the-batch")

    assert result[0]["status"] == "failed"
    assert "The Batch is unavailable for this refresh" in str(result[0]["error"])


def test_batch_normalizes_official_subscription_email_without_scraping() -> None:
    message = EmailMessage()
    message["Subject"] = "The Batch: This week's AI news"
    message["Date"] = "Wed, 29 Oct 2026 10:00:00 +0000"
    message.set_content(
        "Read the issue at https://www.deeplearning.ai/the-batch/issue-334/ and visit the site."
    )
    adapter = batch_adapter(
        imap_host="imap.example.test",
        imap_username="reader@example.test",
        imap_password="app-password",
        imap_from="newsletter@example.test",
    )

    article = adapter._message_to_article(message)

    assert adapter.enabled
    assert article is not None
    assert article.source == "the-batch"
    assert str(article.source_url) == "https://www.deeplearning.ai/the-batch/issue-334/"
    assert article.published_at.isoformat() == "2026-10-29T10:00:00+00:00"


def test_batch_supports_an_html_only_subscription_email() -> None:
    message = EmailMessage()
    message["Subject"] = "The Batch: AI news"
    message["Date"] = "Wed, 29 Oct 2026 10:00:00 +0000"
    message.set_content(
        '<p><a href="https://www.deeplearning.ai/the-batch/issue-335/">Read the issue</a></p>',
        subtype="html",
    )

    article = batch_adapter()._message_to_article(message)

    assert article is not None
    assert str(article.source_url) == "https://www.deeplearning.ai/the-batch/issue-335/"
