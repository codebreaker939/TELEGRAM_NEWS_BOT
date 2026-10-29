# Independent News MCP Server

An independent Python MCP server that collects trusted news, normalizes permitted metadata and feed excerpts, and persists the archive in SQLite. It is being built incrementally before any Telegram bot, LLM, or conversational-agent work begins.

## Current status

**Phase 1, Milestone 1 is live verified.** It provides a working vertical slice for the official MIT News artificial-intelligence RSS feed:

- Streamable HTTP MCP server (and an in-process MCP test path).
- Deterministic LangGraph ingestion workflow: Fetch → Normalize → Validate → Deduplicate → Persist.
- Async SQLite archive with source status, ingestion attempts, stable URL-based IDs, and FTS5 indexing.
- MCP tools: `list_sources`, `refresh_news`, and `get_latest_news`.
- MCP resource: `news://latest`.
- Mocked provider tests for persistence, duplicate refreshes, failure recording, and MCP discovery.

It does **not** yet include Telegram, an LLM, LangChain, the other three providers, full-text query tools, or article/date resources. These are deliberately deferred.

## Architecture

```text
MIT News RSS adapter ─┐
                      ├─ LangGraph ingestion workflow ─ SQLite archive/FTS5 ─ MCP tools/resources
Future adapters ──────┘
```

Provider-specific HTTP/RSS work stays outside the graph. The graph performs deterministic ingestion only; it makes no LLM calls. A future Telegram application will be a separate MCP client and will never call provider adapters directly.

## Requirements and framework roles

- Python 3.11+
- [Official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) — server, tools, resources, and Streamable HTTP transport.
- [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) — the deterministic ingestion state graph.
- SQLite/FTS5 — durable news archive.

LangChain is intentionally not installed in Milestone 1: no agent or LLM integration exists yet, so adding it now would be an unused dependency. It is reserved for the authorized Phase 2 agent.

## Install and configure

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

`NEWS_MCP_DATABASE_PATH` defaults to `data/news.db`. `MIT_NEWS_RSS_URL` defaults to MIT News' public artificial-intelligence topic feed. No API key is needed in this milestone.

## Run the MCP server

```bash
source .venv/bin/activate
python -m news_mcp
```

The Streamable HTTP endpoint is `http://127.0.0.1:8000/mcp` by default. The official SDK recommends Streamable HTTP for a deployed server; stdio is useful for local development but is not enabled by this entry point yet.

### Connect with MCP Inspector

With the server running, open a separate terminal:

```bash
npx -y @modelcontextprotocol/inspector
```

Connect the Inspector to `http://127.0.0.1:8000/mcp`, list tools, run `list_sources`, then run `refresh_news` with `{"source":"mit-news"}`. Finally, invoke `get_latest_news`.

## Test

```bash
source .venv/bin/activate
pytest -q
```

Tests use `httpx.MockTransport`; no live API key or external news request is required. See [docs/ENGINEERING.md](docs/ENGINEERING.md) for the actual verification record and current local environment limitation.

## Environment variables implemented now

| Variable | Purpose | Default |
| --- | --- | --- |
| `NEWS_MCP_DATABASE_PATH` | SQLite archive path | `data/news.db` |
| `MIT_NEWS_RSS_URL` | MIT News AI-topic RSS endpoint | Official MIT News URL |
| `NEWS_MCP_HOST` | HTTP bind host | `127.0.0.1` |
| `NEWS_MCP_PORT` | HTTP bind port | `8000` |

## Known limitations

- Only MIT News is implemented; the current source list accurately exposes only that adapter.
- `search_news`, `get_article`, `get_news_by_date`, and `get_digest_candidates` are not exposed yet.
- RSS publication summaries are stored as short permitted excerpts. Full article bodies are never fetched or stored.
- The MIT adapter requires the standard `curl` executable only when MIT rejects its normal HTTPX RSS request with HTTP 403. macOS includes curl by default.
- `articles_fts` is maintained but is not queried until the search milestone.
- This milestone supports only a single always-on MCP server instance; a later production deployment decision must provide persistent storage for its SQLite archive.

## Planned boundaries

The next recommended milestone is archive retrieval: FTS5-backed `search_news`, ID retrieval, date/digest queries, and their MCP resources/tests. Subsequent Phase 1 work will add independent TechCrunch, Guardian API, and a permitted DeepLearning.AI/The Batch interface after verifying access terms. Phase 2 remains out of scope until explicitly authorized; it will use LangChain, LangGraph persistence, `langchain-mcp-adapters`, and a Telegram MCP client.
