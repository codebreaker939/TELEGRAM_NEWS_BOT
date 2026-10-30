"""MCP-first grounded news agent with an explicitly labeled Groq web-search fallback."""

from __future__ import annotations

import logging
import re
import json
from contextlib import asynccontextmanager
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from news_bot.config import BotSettings

logger = logging.getLogger(__name__)
SOURCE_URL_PATTERN = re.compile(r"https?://[^\s\]\"')]+")

READ_ONLY_NEWS_TOOLS = frozenset(
    {
        "get_latest_news",
        "search_news",
        "get_article",
        "get_related_news",
        "get_news_by_date",
        "get_digest_candidates",
        "list_sources",
    }
)

SYSTEM_PROMPT = """You are a grounded news assistant. Answer news questions only from News MCP
tool results obtained during this request. You MUST call an appropriate tool before making a
factual news claim. The archive contains publisher metadata and permitted excerpts, not article
bodies. Do not invent facts, sources, links, dates, article IDs, or details missing from results.

Use get_latest_news only for a generic request about newest stored coverage, search_news for a
named topic, organization, place, or subject, and
get_article only when an article ID is available. The archive is historical: unless you invoked a
refresh tool (which is not available in this workflow), do not call its contents breaking or live.
If no relevant article is returned, say so plainly. Use this exact Markdown structure:

**Coverage**
Archived News MCP results

**Answer**
Concise grounded answer.

**Sources**
- [Article title](source_url) — publisher — published_at

Never claim that you read a full article."""

WEB_SEARCH_PROMPT = """Use Groq's built-in browser search to answer the user's question because
the News MCP archive has no matching result or is unavailable. Do not describe this as an archive
answer. Do not invent source URLs, dates, authors, or citations. Preserve citations supplied by
the web-search result. Return exactly this concise Markdown structure:

**Coverage**
Live web search via Groq; not stored in the News MCP archive.

**Answer**
Grounded answer in at most five short bullets or one short paragraph.

**Sources**
- Publisher or site — publication date when available — supplied citation or URL
"""


class NewsAssistantUnavailableError(RuntimeError):
    """The LLM, MCP adapter, or bounded agent run was unavailable."""


@dataclass(frozen=True, slots=True)
class ArticleReference:
    article_id: str
    title: str


@dataclass(frozen=True, slots=True)
class AgentAnswer:
    text: str
    article_references: tuple[ArticleReference, ...]
    coverage: str = "archive"


class ToolCallingModel(Protocol):
    def bind_tools(self, tools: list[BaseTool]) -> "ToolCallingModel": ...

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage: ...


class McpAdapter(Protocol):
    async def __aenter__(self) -> "McpAdapter": ...

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    async def list_tools(self) -> list[BaseTool]: ...


ModelFactory = Callable[[BotSettings], ToolCallingModel]
BrowserModelFactory = Callable[[BotSettings], ToolCallingModel]
AdapterFactory = Callable[[str], McpAdapter]


def _create_groq_model(settings: BotSettings) -> ToolCallingModel:
    try:
        from langchain_groq import ChatGroq
    except ImportError as exc:  # pragma: no cover - installation-time failure
        raise NewsAssistantUnavailableError(
            "LangChain Groq support is not installed. Run pip install -e '.[dev]'."
        ) from exc

    return ChatGroq(model=settings.groq_model, temperature=0, max_retries=2, timeout=30)


def _create_browser_search_model(settings: BotSettings) -> ToolCallingModel:
    try:
        from langchain_groq import ChatGroq
    except ImportError as exc:  # pragma: no cover - installation-time failure
        raise NewsAssistantUnavailableError(
            "LangChain Groq support is not installed. Run pip install -e '.[dev]'."
        ) from exc
    # Groq executes this built-in tool server-side and returns the completed response.
    return ChatGroq(
        model=settings.groq_model,
        temperature=0,
        max_retries=2,
        timeout=45,
        reasoning_effort="low",
        max_completion_tokens=1_024,
        tool_choice="required",
        tools=[{"type": "browser_search"}],
    )


def _create_mcp_adapter(server_url: str) -> McpAdapter:
    try:
        from langchain.mcp import MCPAdapter
    except ImportError as exc:  # pragma: no cover - installation-time failure
        raise NewsAssistantUnavailableError(
            "LangChain MCP support is not installed. Run pip install -e '.[dev]'."
        ) from exc
    return MCPAdapter(server_url)


def build_news_graph(
    model: ToolCallingModel,
    tools: list[BaseTool],
    *,
    checkpointer: Any | None = None,
    message_limit: int = 16,
) -> Any:
    """Build the minimal model → MCP tool → model loop for one user request."""
    if not tools:
        raise NewsAssistantUnavailableError("News MCP did not expose any read-only news tools")

    tool_enabled_model = model.bind_tools(tools)

    async def call_model(state: MessagesState) -> dict[str, list[AIMessage]]:
        response = await tool_enabled_model.ainvoke(
            [SystemMessage(content=SYSTEM_PROMPT), *state["messages"][-message_limit:]]
        )
        return {"messages": [response]}

    workflow = StateGraph(MessagesState)
    workflow.add_node("model", call_model)
    workflow.add_node("tools", ToolNode(tools))
    workflow.add_edge(START, "model")
    workflow.add_conditional_edges("model", tools_condition)
    workflow.add_edge("tools", "model")
    return workflow.compile(checkpointer=checkpointer)


class McpNewsAgent:
    """Run grounded requests with a chat-scoped LangGraph checkpoint."""

    def __init__(
        self,
        settings: BotSettings,
        *,
        model_factory: ModelFactory = _create_groq_model,
        browser_model_factory: BrowserModelFactory = _create_browser_search_model,
        adapter_factory: AdapterFactory = _create_mcp_adapter,
        checkpointer: Any | None = None,
    ) -> None:
        self._settings = settings
        self._model_factory = model_factory
        self._browser_model_factory = browser_model_factory
        self._adapter_factory = adapter_factory
        self._checkpointer = checkpointer

    async def answer(self, question: str, *, thread_id: str) -> AgentAnswer:
        if not thread_id:
            raise ValueError("thread_id is required for conversation isolation")
        model = self._model_factory(self._settings)
        try:
            async with self._adapter_factory(self._settings.mcp_server_url) as adapter:
                discovered_tools = await adapter.list_tools()
                news_tools = [
                    tool for tool in discovered_tools if tool.name in READ_ONLY_NEWS_TOOLS
                ]
                if self._checkpointer is None:
                    async with _sqlite_checkpointer(self._settings.checkpoint_database_path) as saver:
                        result = await self._invoke_graph(model, news_tools, question, thread_id, saver)
                else:
                    result = await self._invoke_graph(
                        model, news_tools, question, thread_id, self._checkpointer
                    )
        except NewsAssistantUnavailableError:
            return await self._web_fallback(question)
        except Exception as exc:
            logger.warning("Grounded news-agent request failed: %s", exc)
            return await self._web_fallback(question, cause=exc)

        answer = _final_answer(result.get("messages", []))
        if not answer:
            raise NewsAssistantUnavailableError("The news assistant did not return a response")
        source_urls = _tool_source_urls(result.get("messages", []))
        if source_urls and not any(url in answer for url in source_urls):
            logger.warning("Archive agent returned an uncited response; using Groq web fallback")
            return await self._web_fallback(question)
        if _archive_miss(result.get("messages", [])):
            return await self._web_fallback(question)
        return AgentAnswer(
            text=answer,
            article_references=tuple(_tool_article_references(result.get("messages", []))),
            coverage="archive",
        )

    async def _web_fallback(
        self, question: str, *, cause: Exception | None = None
    ) -> AgentAnswer:
        if not self._settings.web_search_enabled:
            if cause is not None:
                raise NewsAssistantUnavailableError("The news assistant is temporarily unavailable") from cause
            raise NewsAssistantUnavailableError("No matching article is stored in the news archive")
        try:
            response = await self._browser_model_factory(self._settings).ainvoke(
                [SystemMessage(content=WEB_SEARCH_PROMPT), HumanMessage(content=question)]
            )
        except Exception as exc:
            logger.warning("Groq browser-search fallback failed: %s", exc)
            raise NewsAssistantUnavailableError("The news assistant is temporarily unavailable") from exc
        text = _message_text(response)
        if not text:
            raise NewsAssistantUnavailableError("The web search did not return a response")
        return AgentAnswer(text=text, article_references=(), coverage="web_search")

    async def _invoke_graph(
        self,
        model: ToolCallingModel,
        tools: list[BaseTool],
        question: str,
        thread_id: str,
        checkpointer: Any,
    ) -> dict[str, Any]:
        graph = build_news_graph(
            model,
            tools,
            checkpointer=checkpointer,
            message_limit=self._settings.conversation_message_limit,
        )
        return await graph.ainvoke(
            {"messages": [HumanMessage(content=question)]},
            {"configurable": {"thread_id": thread_id}, "recursion_limit": 8},
        )


def _final_answer(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            if isinstance(message.content, str):
                return message.content.strip()
            return str(message.content).strip()
    return ""


def _message_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content.strip()
    return str(message.content).strip()


def _tool_source_urls(messages: list[BaseMessage]) -> set[str]:
    return {
        url.rstrip(".,")
        for message in messages
        if isinstance(message, ToolMessage)
        for url in SOURCE_URL_PATTERN.findall(str(message.content))
    }


def _tool_article_references(messages: list[BaseMessage]) -> list[ArticleReference]:
    references: list[ArticleReference] = []
    seen_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        for value in _json_values(message.content):
            data = value.get("data") if isinstance(value, dict) else None
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                article_id = entry.get("article_id")
                title = entry.get("title")
                if isinstance(article_id, str) and isinstance(title, str) and article_id not in seen_ids:
                    references.append(ArticleReference(article_id=article_id, title=title))
                    seen_ids.add(article_id)
    return references


def _archive_miss(messages: list[BaseMessage]) -> bool:
    """Recognize a structured empty archive response that warrants live web fallback."""
    article_tools = {
        "search_news",
        "get_article",
        "get_news_by_date",
        "get_digest_candidates",
    }
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in article_tools:
            continue
        for value in _json_values(message.content):
            data = value.get("data")
            if data is None or data == []:
                return True
    return False


def _json_values(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, dict):
        return [content]
    if isinstance(content, str):
        try:
            value = json.loads(content)
        except ValueError:
            return []
        return [value] if isinstance(value, dict) else []
    if isinstance(content, list):
        values: list[dict[str, Any]] = []
        for item in content:
            values.extend(_json_values(item.get("text") if isinstance(item, dict) else item))
        return values
    return []


@asynccontextmanager
async def _sqlite_checkpointer(database_path: str):
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError as exc:  # pragma: no cover - installation-time failure
        raise NewsAssistantUnavailableError(
            "LangGraph SQLite checkpoint support is not installed. Run pip install -e '.[dev]'."
        ) from exc

    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(database_path) as saver:
        yield saver
