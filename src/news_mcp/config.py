"""Runtime configuration loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_MIT_NEWS_RSS_URL = "https://news.mit.edu/topic/mitartificial-intelligence2-rss.xml"
DEFAULT_TECHCRUNCH_RSS_URL = "https://techcrunch.com/feed/"
DEFAULT_BATCH_ARCHIVE_URL = "https://www.deeplearning.ai/the-batch"
DEFAULT_BATCH_GOOGLE_NEWS_RSS_URL = (
    "https://news.google.com/rss/search?"
    "q=site%3Adeeplearning.ai%2Fthe-batch%2Fissue-%20%22The%20Batch%22&hl=en-US&gl=US&ceid=US%3Aen"
)


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    mit_news_rss_url: str
    techcrunch_rss_url: str
    guardian_api_key: str | None
    batch_rss_url: str | None
    batch_archive_url: str
    batch_google_news_rss_url: str
    batch_imap_host: str | None
    batch_imap_username: str | None
    batch_imap_password: str | None
    batch_imap_from: str | None
    batch_imap_mailbox: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            database_path=Path(os.getenv("NEWS_MCP_DATABASE_PATH", "data/news.db")),
            mit_news_rss_url=os.getenv("MIT_NEWS_RSS_URL", DEFAULT_MIT_NEWS_RSS_URL),
            techcrunch_rss_url=os.getenv("TECHCRUNCH_RSS_URL", DEFAULT_TECHCRUNCH_RSS_URL),
            guardian_api_key=os.getenv("GUARDIAN_API_KEY") or None,
            batch_rss_url=os.getenv("DEEPLEARNING_AI_BATCH_RSS_URL") or None,
            batch_archive_url=os.getenv("DEEPLEARNING_AI_BATCH_ARCHIVE_URL", DEFAULT_BATCH_ARCHIVE_URL),
            batch_google_news_rss_url=os.getenv(
                "DEEPLEARNING_AI_BATCH_GOOGLE_NEWS_RSS_URL", DEFAULT_BATCH_GOOGLE_NEWS_RSS_URL
            ),
            batch_imap_host=os.getenv("DEEPLEARNING_AI_BATCH_IMAP_HOST") or None,
            batch_imap_username=os.getenv("DEEPLEARNING_AI_BATCH_IMAP_USERNAME") or None,
            batch_imap_password=os.getenv("DEEPLEARNING_AI_BATCH_IMAP_PASSWORD") or None,
            batch_imap_from=os.getenv("DEEPLEARNING_AI_BATCH_IMAP_FROM") or None,
            batch_imap_mailbox=os.getenv("DEEPLEARNING_AI_BATCH_IMAP_MAILBOX", "INBOX"),
            host=os.getenv("NEWS_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("NEWS_MCP_PORT", "8000")),
        )
