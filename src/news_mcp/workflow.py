"""The deterministic LangGraph ingestion workflow."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from news_mcp.adapters import ProviderUnavailableError
from news_mcp.archive import NewsArchive
from news_mcp.models import NewsArticle, RawArticle


class NewsProvider(Protocol):
    source_id: str
    display_name: str
    website_url: str

    async def fetch(self) -> list[RawArticle]: ...


class IngestionState(TypedDict):
    source: str
    attempt_id: str
    raw_articles: list[RawArticle]
    articles: list[NewsArticle]
    fetched_count: int
    persisted_count: int
    duplicate_count: int
    error: str | None


class IngestionService:
    def __init__(self, archive: NewsArchive, providers: list[NewsProvider]) -> None:
        self.archive = archive
        self.providers = {provider.source_id: provider for provider in providers}
        self.graph = self._build_graph()

    async def initialize(self) -> None:
        await self.archive.initialize()
        for provider in self.providers.values():
            await self.archive.register_source(
                provider.source_id, provider.display_name, provider.website_url
            )

    async def list_sources(self) -> list[dict[str, object]]:
        await self.initialize()
        return await self.archive.list_sources()

    async def refresh(self, source: str | None = None) -> list[dict[str, object]]:
        await self.initialize()
        if source and source not in self.providers:
            raise ValueError(f"Unknown source: {source}")
        source_ids = [source] if source else list(self.providers)
        return [await self._refresh_one(source_id) for source_id in source_ids]

    async def get_latest(
        self, *, source: str | None = None, category: str | None = None, limit: int = 10
    ) -> list[NewsArticle]:
        await self.initialize()
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if source and source not in self.providers:
            raise ValueError(f"Unknown source: {source}")
        return await self.archive.get_latest(source=source, category=category, limit=limit)

    async def _refresh_one(self, source: str) -> dict[str, object]:
        attempt_id = str(uuid.uuid4())
        await self.archive.start_attempt(attempt_id, source, datetime.now(UTC))
        initial_state: IngestionState = {
            "source": source,
            "attempt_id": attempt_id,
            "raw_articles": [],
            "articles": [],
            "fetched_count": 0,
            "persisted_count": 0,
            "duplicate_count": 0,
            "error": None,
        }
        try:
            result = await self.graph.ainvoke(initial_state)
        except Exception as exc:
            await self.archive.finish_attempt(
                attempt_id,
                source,
                status="failed",
                fetched_count=0,
                persisted_count=0,
                duplicate_count=0,
                error_message=str(exc),
            )
            return {"source": source, "status": "failed", "error": str(exc)}

        status = "failed" if result["error"] else "success"
        await self.archive.finish_attempt(
            attempt_id,
            source,
            status=status,
            fetched_count=result["fetched_count"],
            persisted_count=result["persisted_count"],
            duplicate_count=result["duplicate_count"],
            error_message=result["error"],
        )
        return {
            "source": source,
            "status": status,
            "fetched_count": result["fetched_count"],
            "persisted_count": result["persisted_count"],
            "duplicate_count": result["duplicate_count"],
            "error": result["error"],
        }

    def _build_graph(self):
        async def fetch(state: IngestionState) -> dict[str, object]:
            try:
                raw_articles = await self.providers[state["source"]].fetch()
            except ProviderUnavailableError as exc:
                return {"error": str(exc)}
            return {"raw_articles": raw_articles, "fetched_count": len(raw_articles)}

        def normalize(state: IngestionState) -> dict[str, object]:
            if state["error"]:
                return {}
            fetched_at = datetime.now(UTC)
            return {"articles": [NewsArticle.from_raw(item, fetched_at) for item in state["raw_articles"]]}

        def validate(state: IngestionState) -> dict[str, object]:
            if state["error"]:
                return {}
            valid_articles: list[NewsArticle] = []
            for article in state["articles"]:
                try:
                    valid_articles.append(NewsArticle.model_validate(article))
                except ValueError:
                    continue
            return {"articles": valid_articles}

        def deduplicate(state: IngestionState) -> dict[str, object]:
            if state["error"]:
                return {}
            unique: dict[str, NewsArticle] = {}
            for article in state["articles"]:
                unique.setdefault(article.article_id, article)
            return {"articles": list(unique.values())}

        async def persist(state: IngestionState) -> dict[str, object]:
            if state["error"]:
                return {}
            inserted, duplicates = await self.archive.persist_articles(state["articles"])
            return {"persisted_count": inserted, "duplicate_count": duplicates}

        graph = StateGraph(IngestionState)
        graph.add_node("fetch", fetch)
        graph.add_node("normalize", normalize)
        graph.add_node("validate", validate)
        graph.add_node("deduplicate", deduplicate)
        graph.add_node("persist", persist)
        graph.add_edge(START, "fetch")
        graph.add_edge("fetch", "normalize")
        graph.add_edge("normalize", "validate")
        graph.add_edge("validate", "deduplicate")
        graph.add_edge("deduplicate", "persist")
        graph.add_edge("persist", END)
        return graph.compile()
