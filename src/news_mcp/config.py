"""Runtime configuration loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_MIT_NEWS_RSS_URL = "https://news.mit.edu/topic/mitartificial-intelligence2-rss.xml"


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    mit_news_rss_url: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            database_path=Path(os.getenv("NEWS_MCP_DATABASE_PATH", "data/news.db")),
            mit_news_rss_url=os.getenv("MIT_NEWS_RSS_URL", DEFAULT_MIT_NEWS_RSS_URL),
            host=os.getenv("NEWS_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("NEWS_MCP_PORT", "8000")),
        )
