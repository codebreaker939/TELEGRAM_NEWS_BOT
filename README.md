# Independent News MCP Server

An independent Python MCP server that collects trusted news, normalizes permitted metadata and feed excerpts, and persists the archive in SQLite. Phase 2 adds a separately executable Telegram MCP client and conversational agent without coupling it to the MCP server internals.

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
- Phase 2 Milestone 4 `news_bot` package: authorized `/start`, `/news`, `/digest`, and `/history`; grounded conversations; persistent memory; and a restart-aware 6:00 AM Asia/Kolkata digest job.
- Post-Milestone-4 web fallback: when a topic search returns no matching archived article, Groq built-in Browser Search provides a clearly labeled live-web answer.

Milestone 4 adds `/refresh` for an authorized manual MCP provider refresh, selects recent undelivered articles, and schedules a daily digest at 6:00 AM Asia/Kolkata. It records each recipient/date claim and completed article-ID delivery to prevent normal restart duplicates. `/digest` runs the same refresh → select → send workflow for manual testing. The post-Milestone-4 fallback uses the configured Groq `openai/gpt-oss-20b` model's built-in Browser Search only after a matching MCP topic query is empty. The Guardian adapter needs `GUARDIAN_API_KEY` before it can fetch live content. The Batch tries publisher-provided public archive JSON-LD metadata first, then a tightly validated Google News RSS discovery fallback, and finally an optional subscription inbox. It never fetches newsletter issue pages or invents an RSS endpoint.

## Architecture

```text
MIT News RSS adapter ───┐
TechCrunch RSS adapter ─┼─ LangGraph ingestion workflow ─ SQLite archive/FTS5 ─ MCP tools/resources
Guardian API adapter ───┤
The Batch adapter ──────┘

Telegram Bot process (`news_bot`)
  ├─ `/news` ─ official MCP Streamable HTTP client ──> `news_mcp` process
  ├─ `/history` ─ delivery article IDs ─ official MCP client ──> `news_mcp` process
  └─ text/button ─ LangGraph checkpoint → ChatGroq → MCPAdapter ──> `news_mcp` process
       └─ separate bot SQLite: delivery references + LangGraph checkpoints
```

Provider-specific HTTP/RSS work stays outside the graph. The graph performs deterministic ingestion only; it makes no LLM calls. `news_bot` is a separate process and only reaches news through the MCP protocol; it never imports provider adapters or accesses the Phase 1 SQLite archive.

## Requirements and framework roles

- Python 3.11+
- [Official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) — server, tools, resources, and Streamable HTTP transport.
- [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) — deterministic ingestion, the agent model/tool loop, and Milestone 3 conversation checkpoints.
- [LangChain](https://docs.langchain.com/oss/python/) and [langchain-groq](https://reference.langchain.com/python/langchain-groq/) — ChatGroq model integration and tool calling.
- [Groq Browser Search](https://console.groq.com/docs/tool-use/built-in-tools/browser-search) — explicit live-web fallback for an empty MCP archive topic search.
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

## Run the Telegram bot — Phase 2 Milestone 4

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
NEWS_BOT_WEB_SEARCH_ENABLED=true
NEWS_BOT_DATABASE_PATH=data/news_bot.db
NEWS_BOT_CHECKPOINT_DATABASE_PATH=data/news_bot_checkpoints.db
NEWS_BOT_CONVERSATION_MESSAGE_LIMIT=16
LANGGRAPH_STRICT_MSGPACK=true
TELEGRAM_DIGEST_CHAT_IDS=your-telegram-user-or-private-chat-id
NEWS_BOT_DIGEST_LIMIT=5
NEWS_BOT_DIGEST_CANDIDATE_LIMIT=50
```

Start the MCP server first. In a separate terminal, start the bot:

```bash
source .venv/bin/activate
python -m news_bot
```

The bot must run in a separate process from the MCP server. `/news` remains deterministic and calls `get_latest_news` through the official MCP SDK client; after delivery it stores only the returned article IDs, their order, and user/chat delivery metadata in the bot database. `/history` rereads those IDs through MCP `get_article`, never through Phase 1 SQLite. Ordinary text and follow-up buttons run the LangGraph agent with a per-chat/user checkpoint thread. It discovers tools through `MCPAdapter`, permits only read-only archive tools, and instructs the model to cite title, publisher, publication date, and original URL from retrieved records. A structured empty topic-search result invokes Groq Browser Search when enabled; the Telegram message is visibly labeled as live web coverage rather than archive coverage. It never imports Phase 1 provider code or queries its database directly.

At startup the Telegram JobQueue schedules one job per digest recipient at 06:00 with an `Asia/Kolkata` timezone-aware time. `/refresh` invokes only MCP `refresh_news` and reports source counts. `/digest` invokes the same refresh → select → send → record workflow immediately for the requesting chat. Set `TELEGRAM_DIGEST_CHAT_IDS` explicitly in a multi-user deployment; if unset, the bot uses the union of authorized user and chat IDs. An always-on process with durable `data/` storage is required; a laptop that sleeps or stops cannot reliably deliver a 6 AM digest.

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
| `NEWS_BOT_WEB_SEARCH_ENABLED` | Use Groq Browser Search after a structured empty topic-search result | `true` |
| `NEWS_BOT_DATABASE_PATH` | Bot-owned SQLite delivery-reference database | `data/news_bot.db` |
| `NEWS_BOT_CHECKPOINT_DATABASE_PATH` | LangGraph SQLite checkpoint database | `data/news_bot_checkpoints.db` |
| `NEWS_BOT_CONVERSATION_MESSAGE_LIMIT` | Maximum prior graph messages sent to the model | `16`, range 4–40 |
| `LANGGRAPH_STRICT_MSGPACK` | Enables LangGraph's strict checkpoint serializer mode | `true` recommended for new checkpoint databases |
| `TELEGRAM_DIGEST_CHAT_IDS` | Comma-separated recipient chat IDs for scheduled digests | Authorized user/chat ID union when unset |
| `NEWS_BOT_DIGEST_LIMIT` | Maximum newly eligible articles per digest | `5`, range 1–10 |
| `NEWS_BOT_DIGEST_CANDIDATE_LIMIT` | Latest MCP archive records inspected for eligible articles | `50`, range digest limit–100 |

## Known limitations

- RSS publication summaries are stored as short permitted excerpts. Full article bodies are never fetched or stored.
- The MIT adapter requires the standard `curl` executable only when MIT rejects its normal HTTPX RSS request with HTTP 403. macOS includes curl by default.
- FTS search is limited to persisted titles, feed excerpts, keywords, and source names; full article bodies are never indexed.
- This milestone supports only a single always-on MCP server instance; a later production deployment decision must provide persistent storage for its SQLite archive.
- The Batch public archive can deny automated requests. A denied/invalid archive automatically falls through to Google News; Google items are stored only if their redirect ends at an official Batch issue URL. A valid but empty Google feed is recorded as a successful zero-article refresh (no new issue); transport/invalid-response failures are recorded as `failed` while the other sources continue.
- The Batch inbox mode needs an actual subscribed newsletter, its exact sender address, and an IMAP app password. It stores email metadata/excerpts and the official issue URL only; it does not retrieve the linked newsletter page.
- Phase 2 Milestone 1 has live-verified Telegram authentication, polling, authorized `/start`, and authorized `/news` delivery from the persisted MCP archive on the developer's Mac. The bot fails closed when no authorized user/chat configuration exists.
- Daily delivery claims are stored before retrieval and marked `sent` only after Telegram accepts the message and ordered IDs are persisted. This prevents normal restart duplicates. As with any external API plus local database, a process crash after Telegram accepts a message but before SQLite commits is an unavoidable at-least-once edge case; the active claim lease reduces duplicate retries and is documented for operators.
- The digest considers articles from the last 36 hours, excludes any article ID previously delivered to that recipient, and removes exact normalized-title duplicates. It is deterministic and does not invoke an LLM.
- The agent's model context is bounded, but the checkpoint database itself grows until a future retention policy is added. Its read-only tool set cannot refresh providers; answers describe the stored archive rather than claim breaking/live coverage.
- Browser Search sends the user's fallback question to Groq's server-side search service and is not the News MCP archive. Groq documents Browser Search as incompatible with API structured-output mode, so the bot enforces a readable Markdown structure and a visible coverage label rather than claiming schema-validated web output.
- Milestone 3 mocked tests cover the Telegram → LangGraph → MCP-tool flow, checkpoint thread isolation, delivery-order persistence, and MCP-based historical rereads. Live Groq/MCPAdapter and SQLite-checkpointer verification remain pending installation of the newly declared dependencies in a network-enabled environment and a configured `GROQ_API_KEY`.

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

Live MCP verification on the developer's Mac succeeded for MIT News, TechCrunch, and The Guardian. The Batch archive metadata and Google News discovery fallback currently do not yield a verifiable official issue URL, so it is accurately recorded as unavailable for that refresh without interrupting the other sources. Its optional subscription inbox remains available. Phase 2 Milestones 1–4 and the Groq Browser Search fallback extension are implemented. Production-readiness work remains Milestone 5.
