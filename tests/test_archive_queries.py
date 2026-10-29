from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from news_mcp.archive import NewsArchive
from news_mcp.models import NewsArticle, article_id_for_url


def article(
    title: str, slug: str, published_at: datetime, description: str, *, source: str = "mit-news"
) -> NewsArticle:
    domain = "news.mit.edu" if source == "mit-news" else "www.theguardian.com"
    url = f"https://{domain}/2026/{slug}"
    return NewsArticle(
        article_id=article_id_for_url(url),
        title=title,
        source=source,
        author="MIT News",
        category="artificial-intelligence",
        description=description,
        source_url=url,
        published_at=published_at,
        fetched_at=datetime(2026, 10, 29, tzinfo=UTC),
        keywords=["Artificial intelligence"],
        original_summary=description,
        content_status="excerpt_only",
    )


@pytest.mark.asyncio
async def test_archive_search_article_and_date_queries(tmp_path: Path) -> None:
    archive = NewsArchive(tmp_path / "news.db")
    await archive.register_source("mit-news", "MIT News", "https://news.mit.edu")
    chip_story = article(
        "OpenAI announces an AI chip",
        "openai-ai-chip",
        datetime(2026, 10, 28, 16, tzinfo=UTC),
        "Researchers describe a chip designed for AI workloads.",
    )
    safety_story = article(
        "AI safety research",
        "ai-safety",
        datetime(2026, 10, 28, 9, tzinfo=UTC),
        "A new approach improves AI safety evaluation.",
    )
    older_story = article(
        "Machine learning methods",
        "machine-learning-methods",
        datetime(2026, 10, 27, 9, tzinfo=UTC),
        "Machine learning researchers publish a new method.",
    )
    await archive.persist_articles([chip_story, safety_story, older_story])

    search_results = await archive.search("OpenAI chip")
    by_id = await archive.get_article(chip_story.article_id)
    by_date = await archive.get_by_date(datetime(2026, 10, 28, tzinfo=UTC).date())
    digest = await archive.get_digest_candidates(datetime(2026, 10, 28, tzinfo=UTC).date(), limit=1)
    ranged_search = await archive.search(
        "AI", from_date=datetime(2026, 10, 28, tzinfo=UTC).date()
    )

    assert [item.article_id for item in search_results] == [chip_story.article_id]
    assert by_id == chip_story
    assert [item.article_id for item in by_date] == [chip_story.article_id, safety_story.article_id]
    assert [item.article_id for item in digest] == [chip_story.article_id]
    assert {item.article_id for item in ranged_search} == {
        chip_story.article_id,
        safety_story.article_id,
    }


@pytest.mark.asyncio
async def test_archive_rejects_empty_fts_query(tmp_path: Path) -> None:
    archive = NewsArchive(tmp_path / "news.db")

    with pytest.raises(ValueError, match="at least one letter or number"):
        await archive.search("---")


@pytest.mark.asyncio
async def test_archive_persists_across_reopen_and_honors_source_date_filters(tmp_path: Path) -> None:
    path = tmp_path / "news.db"
    first_archive = NewsArchive(path)
    await first_archive.register_source("mit-news", "MIT News", "https://news.mit.edu")
    await first_archive.register_source("guardian", "The Guardian", "https://www.theguardian.com")
    day = datetime(2026, 10, 28, tzinfo=UTC)
    mit_story = article("AI research from MIT", "mit-ai", day, "AI research from MIT.")
    guardian_story = article(
        "AI policy from The Guardian",
        "guardian-ai",
        day,
        "AI policy coverage.",
        source="guardian",
    )
    await first_archive.persist_articles([mit_story, guardian_story])

    reopened_archive = NewsArchive(path)
    by_date = await reopened_archive.get_by_date(day.date(), source="guardian")
    source_search = await reopened_archive.search("AI", source="mit-news")

    assert by_date == [guardian_story]
    assert source_search == [mit_story]


@pytest.mark.asyncio
async def test_archive_identifies_similar_cross_source_titles(tmp_path: Path) -> None:
    archive = NewsArchive(tmp_path / "news.db")
    await archive.register_source("mit-news", "MIT News", "https://news.mit.edu")
    await archive.register_source("guardian", "The Guardian", "https://www.theguardian.com")
    mit_story = article(
        "OpenAI announces new AI chip for data centers",
        "openai-chip",
        datetime(2026, 10, 28, tzinfo=UTC),
        "MIT coverage.",
    )
    guardian_story = article(
        "OpenAI announces new AI chip for data centers",
        "technology/openai-chip",
        datetime(2026, 10, 29, tzinfo=UTC),
        "Guardian coverage.",
        source="guardian",
    )
    await archive.persist_articles([mit_story, guardian_story])

    related = await archive.get_related_articles(mit_story.article_id)

    assert len(related) == 1
    assert related[0]["article"] == guardian_story
    assert related[0]["reason"] == "title token overlap"
