"""Grounded LangGraph news agent backed exclusively by MCP-discovered tools."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
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

Use get_latest_news for a request about the newest stored coverage, search_news for a topic, and
get_article only when an article ID is available. The archive is historical: unless you invoked a
refresh tool (which is not available in this workflow), do not call its contents breaking or live.
If no relevant article is returned, say so plainly. In every answer based on an article, include a
compact Sources section with each article title, publisher, published_at value, and source_url.
Keep the response concise and never claim that you read a full article."""


class NewsAssistantUnavailableError(RuntimeError):
    """The LLM, MCP adapter, or bounded agent run was unavailable."""


class ToolCallingModel(Protocol):
    def bind_tools(self, tools: list[BaseTool]) -> "ToolCallingModel": ...

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage: ...


class McpAdapter(Protocol):
    async def __aenter__(self) -> "McpAdapter": ...

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    async def list_tools(self) -> list[BaseTool]: ...


ModelFactory = Callable[[BotSettings], ToolCallingModel]
AdapterFactory = Callable[[str], McpAdapter]


def _create_groq_model(settings: BotSettings) -> ToolCallingModel:
    try:
        from langchain_groq import ChatGroq
    except ImportError as exc:  # pragma: no cover - installation-time failure
        raise NewsAssistantUnavailableError(
            "LangChain Groq support is not installed. Run pip install -e '.[dev]'."
        ) from exc

    return ChatGroq(model=settings.groq_model, temperature=0, max_retries=2, timeout=30)


def _create_mcp_adapter(server_url: str) -> McpAdapter:
    try:
        from langchain.mcp import MCPAdapter
    except ImportError as exc:  # pragma: no cover - installation-time failure
        raise NewsAssistantUnavailableError(
            "LangChain MCP support is not installed. Run pip install -e '.[dev]'."
        ) from exc
    return MCPAdapter(server_url)


def build_news_graph(model: ToolCallingModel, tools: list[BaseTool]) -> Any:
    """Build the minimal model → MCP tool → model loop for one user request."""
    if not tools:
        raise NewsAssistantUnavailableError("News MCP did not expose any read-only news tools")

    tool_enabled_model = model.bind_tools(tools)

    async def call_model(state: MessagesState) -> dict[str, list[AIMessage]]:
        response = await tool_enabled_model.ainvoke(
            [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        )
        return {"messages": [response]}

    workflow = StateGraph(MessagesState)
    workflow.add_node("model", call_model)
    workflow.add_node("tools", ToolNode(tools))
    workflow.add_edge(START, "model")
    workflow.add_conditional_edges("model", tools_condition)
    workflow.add_edge("tools", "model")
    return workflow.compile()


class McpNewsAgent:
    """Run one grounded news request without retaining conversation state."""

    def __init__(
        self,
        settings: BotSettings,
        *,
        model_factory: ModelFactory = _create_groq_model,
        adapter_factory: AdapterFactory = _create_mcp_adapter,
    ) -> None:
        self._settings = settings
        self._model_factory = model_factory
        self._adapter_factory = adapter_factory

    async def answer(self, question: str) -> str:
        model = self._model_factory(self._settings)
        try:
            async with self._adapter_factory(self._settings.mcp_server_url) as adapter:
                discovered_tools = await adapter.list_tools()
                news_tools = [
                    tool for tool in discovered_tools if tool.name in READ_ONLY_NEWS_TOOLS
                ]
                graph = build_news_graph(model, news_tools)
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content=question)]},
                    {"recursion_limit": 8},
                )
        except NewsAssistantUnavailableError:
            raise
        except Exception as exc:
            logger.warning("Grounded news-agent request failed: %s", exc)
            raise NewsAssistantUnavailableError("The news assistant is temporarily unavailable") from exc

        answer = _final_answer(result.get("messages", []))
        if not answer:
            raise NewsAssistantUnavailableError("The news assistant did not return a response")
        source_urls = _tool_source_urls(result.get("messages", []))
        if source_urls and not any(url in answer for url in source_urls):
            raise NewsAssistantUnavailableError("The news assistant did not return a cited response")
        return answer


def _final_answer(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            if isinstance(message.content, str):
                return message.content.strip()
            return str(message.content).strip()
    return ""


def _tool_source_urls(messages: list[BaseMessage]) -> set[str]:
    return {
        url.rstrip(".,")
        for message in messages
        if isinstance(message, ToolMessage)
        for url in SOURCE_URL_PATTERN.findall(str(message.content))
    }
