# Independent News MCP Server

An independent Python MCP server that collects trusted news, normalizes permitted metadata and feed excerpts, and persists the archive in SQLite. Phase 2 now adds a separately executable Telegram MCP client, beginning with the minimal authorized `/start` and `/news` connectivity milestone.

## Current status

**Phase 1 is technically ready for the three live-verified providers and partially complete against the original four-source requirement.** The server currently provides:

- Streamable HTTP MCP server (and an in-process MCP test path).
- Deterministic LangGraph ingestion workflow: Fetch → Normalize → Validate → Deduplicate → Persist.
- Async SQLite archive with source status, ingestion attempts, stable URL-based IDs, and FTS5 indexing.
- MCP tools: `list_sources`, `refresh_news`, `get_latest_news`, `search_news`, `get_article`, `get_related_news`, `get_news_by_date`, and `get_digest_candidates`.
- MCP resources: `news://latest`, `news://article/{article_id}`, and `news://archive/{date}`.
- Independent adapters for MIT News, TechCrunch RSS, and the Guardian Open Platform API.
- Source configuration/status and persisted ingestion history for all four required publishers.
- URL deduplication and persisted potential cross-source duplicate candidates based on same-period title similarity.
- Mocked provider, archive-query, and MCP-discovery tests.
- Phase 2 Milestone 2 `news_bot` package: authorized `/start` and deterministic `/news`, plus grounded ordinary-text news questions through LangGraph, LangChain, and MCP-discovered tools.

Milestone 2 adds a stateless grounded agent only. Persistent conversational memory, delivery tracking, `/digest`, `/history`, `/help`, suggested follow-up buttons, and scheduling remain deferred to explicitly authorized milestones. The Guardian adapter needs `GUARDIAN_API_KEY` before it can fetch live content. The Batch tries publisher-provided public archive JSON-LD metadata first, then a tightly validated Google News RSS discovery fallback, and finally an optional subscription inbox. It never fetches newsletter issue pages or invents an RSS endpoint.

## Architecture

```text
MIT News RSS adapter ───┐
TechCrunch RSS adapter ─┼─ LangGraph ingestion workflow ─ SQLite archive/FTS5 ─ MCP tools/resources
Guardian API adapter ───┤
The Batch adapter ──────┘

Telegram Bot process (`news_bot`)
  ├─ `/news` ─ official MCP Streamable HTTP client ──> `news_mcp` process
  └─ text message ─ LangGraph → ChatGroq → MCPAdapter ──> `news_mcp` process
```

Provider-specific HTTP/RSS work stays outside the graph. The graph performs deterministic ingestion only; it makes no LLM calls. `news_bot` is a separate process and only reaches news through the MCP protocol; it never imports provider adapters or accesses the Phase 1 SQLite archive.

## Requirements and framework roles

- Python 3.11+
- [Official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) — server, tools, resources, and Streamable HTTP transport.
- [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) — deterministic ingestion and the Milestone 2 model/tool loop.
- [LangChain](https://docs.langchain.com/oss/python/) and [langchain-groq](https://reference.langchain.com/python/langchain-groq/) — ChatGroq model integration and tool calling.
- [LangChain MCPAdapter](https://docs.langchain.com/oss/python/langchain/mcp) — MCP tool discovery over the existing local Streamable HTTP endpoint.
- [python-telegram-bot](https://docs.python-telegram-bot.org/en/stable/) — async Telegram command handling.
- SQLite/FTS5 — durable news archive.

Milestone 2 uses Groq's `openai/gpt-oss-20b` through `ChatGroq`. Its function-calling requests are executed only inside this application using read-only MCP-adapted tools; no provider adapter or Phase 1 database is imported by the bot.

## Install and configure

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

`NEWS_MCP_DATABASE_PATH` defaults to `data/news.db`. `MIT_NEWS_RSS_URL` defaults to MIT News' public artificial-intelligence topic feed. Set `GUARDIAN_API_KEY` to enable the Guardian. The Batch's public archive and Google News fallback are configured by default. You may additionally configure an IMAP mailbox that receives your own The Batch subscription, using a mail-provider app password rather than your normal mailbox password.

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

## Run the Telegram bot — Phase 2 Milestone 2

Configure these values in `.env` before starting the bot:

```dotenv
TELEGRAM_BOT_TOKEN=token-from-botfather
TELEGRAM_ALLOWED_USER_IDS=your-telegram-user-id
# Optional additional restriction, for example a private/group chat ID.
TELEGRAM_ALLOWED_CHAT_IDS=
NEWS_BOT_MCP_URL=http://127.0.0.1:8000/mcp
NEWS_BOT_LATEST_LIMIT=5
GROQ_API_KEY=your-groq-api-key
GROQ_MODEL=openai/gpt-oss-20b
```

Start the MCP server first. In a separate terminal, start the bot:

```bash
source .venv/bin/activate
python -m news_bot
```

The bot must run in a separate process from the MCP server. `/news` remains deterministic and calls `get_latest_news` through the official MCP SDK client. Ordinary text such as `What is the latest AI news?` runs the stateless LangGraph agent. It discovers tools through `MCPAdapter`, permits only read-only archive tools, and instructs the model to cite title, publisher, publication date, and original URL from retrieved records. It never imports Phase 1 provider code or queries its database directly.

The bot suppresses `httpx`/`httpcore` INFO logs because Telegram’s API URL contains the bot token. It retries Telegram bootstrap failures up to three times before exiting; a persistent timeout still requires network diagnosis. If a token is ever shown in a terminal, chat, commit, or screenshot, revoke it in `@BotFather`, replace it in `.env`, and restart the bot.

## Test

```bash
source .venv/bin/activate
pytest -q
```

Tests use mocked transports/clients; no live API key, Telegram token, or external news request is required. See [docs/ENGINEERING.md](docs/ENGINEERING.md) for actual verification records and pending external checks.

## Environment variables implemented now

| Variable | Purpose | Default |
| --- | --- | --- |
| `NEWS_MCP_DATABASE_PATH` | SQLite archive path | `data/news.db` |
| `MIT_NEWS_RSS_URL` | MIT News AI-topic RSS endpoint | Official MIT News URL |
| `TECHCRUNCH_RSS_URL` | TechCrunch public RSS endpoint | `https://techcrunch.com/feed/` |
| `GUARDIAN_API_KEY` | Official Guardian Open Platform key | Unset; Guardian disabled |
| `DEEPLEARNING_AI_BATCH_RSS_URL` | Publisher-authorized The Batch RSS/Atom endpoint | Unset |
| `DEEPLEARNING_AI_BATCH_ARCHIVE_URL` | Public The Batch archive metadata page | `https://www.deeplearning.ai/the-batch` |
| `DEEPLEARNING_AI_BATCH_GOOGLE_NEWS_RSS_URL` | Constrained Google News RSS discovery fallback | Google News site query |
| `DEEPLEARNING_AI_BATCH_IMAP_HOST` | IMAPS hostname for an inbox receiving The Batch | Unset |
| `DEEPLEARNING_AI_BATCH_IMAP_USERNAME` | IMAP account username | Unset |
| `DEEPLEARNING_AI_BATCH_IMAP_PASSWORD` | IMAP app password; never the primary mailbox password | Unset |
| `DEEPLEARNING_AI_BATCH_IMAP_FROM` | Exact sender address of the subscribed The Batch email | Unset |
| `DEEPLEARNING_AI_BATCH_IMAP_MAILBOX` | Read-only IMAP mailbox to search | `INBOX` |
| `NEWS_MCP_HOST` | HTTP bind host | `127.0.0.1` |
| `NEWS_MCP_PORT` | HTTP bind port | `8000` |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token | Required for `news_bot` |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated authorized Telegram user IDs | At least this or allowed chat IDs is required |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated authorized Telegram chat IDs | Optional additional restriction |
| `NEWS_BOT_MCP_URL` | Independent News MCP Streamable HTTP endpoint | `http://127.0.0.1:8000/mcp` |
| `NEWS_BOT_LATEST_LIMIT` | Maximum persisted articles included by `/news` | `5`, range 1–10 |
| `GROQ_API_KEY` | Groq API credential used only by `ChatGroq` | Required for `news_bot` |
| `GROQ_MODEL` | Groq chat model with function calling | Required; `openai/gpt-oss-20b` |

## Known limitations

- RSS publication summaries are stored as short permitted excerpts. Full article bodies are never fetched or stored.
- The MIT adapter requires the standard `curl` executable only when MIT rejects its normal HTTPX RSS request with HTTP 403. macOS includes curl by default.
- FTS search is limited to persisted titles, feed excerpts, keywords, and source names; full article bodies are never indexed.
- This milestone supports only a single always-on MCP server instance; a later production deployment decision must provide persistent storage for its SQLite archive.
- The Batch public archive can deny automated requests. A denied/invalid archive automatically falls through to Google News; Google items are stored only if their redirect ends at an official Batch issue URL. A valid but empty Google feed is recorded as a successful zero-article refresh (no new issue); transport/invalid-response failures are recorded as `failed` while the other sources continue.
- The Batch inbox mode needs an actual subscribed newsletter, its exact sender address, and an IMAP app password. It stores email metadata/excerpts and the official issue URL only; it does not retrieve the linked newsletter page.
- Phase 2 Milestone 1 has live-verified Telegram authentication, polling, authorized `/start`, and authorized `/news` delivery from the persisted MCP archive on the developer's Mac. The bot fails closed when no authorized user/chat configuration exists.
- Milestone 2 is stateless: it cannot yet resolve references such as “the second article” from an earlier message. Its read-only tool set cannot refresh providers; answers describe the stored archive rather than claim breaking/live coverage.
- The Milestone 2 mocked Telegram → LangGraph → MCP-tool → Telegram test passes. Live Groq/MCPAdapter verification remains pending installation of the newly declared dependencies in a network-enabled environment and a configured `GROQ_API_KEY`.

## Phase 1 acceptance checklist

| Requirement | Status | Evidence |
| --- | --- | --- |
| Independent Streamable HTTP MCP server, discovery, tools, and resources | Pass | Live MCP Inspector runs on macOS; in-process MCP resource tests cover populated `latest`, article, and date resources. |
| Real articles from available providers | Pass for MIT News, TechCrunch, and The Guardian | Live refresh persisted 50 MIT, 20 TechCrunch, and 50 Guardian articles. |
| Original four-source retrieval requirement | **Partial** | The Batch archive metadata and Google News fallback did not yield a verifiable official issue URL. No content was fabricated or scraped. |
| Stable schema, SQLite persistence, historical/date/source retrieval | Pass | Stable URL IDs, FTS5, source/date filters, and SQLite reopen are tested; live MCP query results returned stored MIT and Guardian articles. |
| Repeat refresh deduplication | Pass for live-verified providers | Second live refreshes produced zero inserts and 50/50/20 duplicates for MIT/Guardian/TechCrunch. |
| One provider failure does not stop others | Pass | The Batch failed independently while the other three sources persisted successfully. |
| Timeout, rate-limit, malformed response, and missing-configuration handling | Pass in mocked automated tests | Provider errors become recorded failed/skipped attempts; no fabricated records are persisted. |
| Automated suite and documentation | Pass | `21 passed`; this README and the engineering record contain actual verification results and limitations. |

Phase 1 cannot be signed off as fully satisfying the original four-source ingestion objective until The Batch has a permitted, working acquisition method. Configuring the optional subscription inbox is the current practical route.

## Planned boundaries

Live MCP verification on the developer's Mac succeeded for MIT News, TechCrunch, and The Guardian. The Batch archive metadata and Google News discovery fallback currently do not yield a verifiable official issue URL, so it is accurately recorded as unavailable for that refresh without interrupting the other sources. Its optional subscription inbox remains available. Phase 2 Milestones 1 and 2 are implemented; persistent memory, delivery records, suggested follow-ups, and scheduling remain out of scope until separately authorized.
