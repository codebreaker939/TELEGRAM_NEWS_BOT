# Engineering record

## Scope and implementation history

### 2026-10-29 — Phase 1, Milestone 1: archive and MIT News vertical slice

Implemented the initial independent MCP-server vertical slice. It handles a public MIT News artificial-intelligence topic RSS feed, stores only publisher metadata and a stripped feed excerpt, and exposes the persisted archive over Streamable HTTP MCP.

This is not Phase 2: no Telegram code, LLM provider, LangChain agent, user memory, delivery records, or `langchain-mcp-adapters` have been added.

### 2026-10-29 — Phase 1, Milestone 2: archive retrieval surface

Added the persisted archive query functionality without adding providers or an LLM. SQLite FTS5 now powers keyword/topic search over the stored title, excerpt, keywords, and source. Stable-ID lookup, UTC-date retrieval, digest candidates, and dynamic article/date MCP resource templates are implemented.

### 2026-10-30 — Phase 1, provider expansion and duplicate candidates

Added independent TechCrunch RSS and Guardian Open Platform adapters, plus a source-status entry for DeepLearning.AI's The Batch. The Batch was initially disabled because investigation found its public newsletter/archive pages but no documented publisher RSS/Atom/API interface; later milestones added its strictly validated public-discovery and optional mailbox paths. Added persisted potential cross-source duplicate candidates based on title similarity within a three-day publication window.

### 2026-10-30 — Phase 1, The Batch authorized subscription ingestion

The Batch adapter now supports two explicit, permitted acquisition methods: a publisher-authorized RSS/Atom endpoint, or a deployment-owned IMAP inbox that receives the user's official The Batch subscription. The IMAP path searches at most ten recent messages from the configured sender in read-only mode, extracts a matching official issue URL from plain-text or HTML email, and stores only the email subject, timestamp, short text excerpt, and issue URL. It never fetches issue/archive HTML or linked article pages. All IMAP values are environment-only settings; the password must be a provider-generated app password.

### 2026-10-30 — Phase 1, The Batch public archive and Google News fallback

The Batch adapter now attempts public archive JSON-LD metadata before Google News RSS discovery. Archive parsing is deliberately limited to publisher-exposed `application/ld+json` fields with a valid issue URL, headline/name, and timezone-aware `datePublished`; it does not parse article or newsletter bodies. Google News is not treated as a source of truth: each result is accepted only when its redirect chain terminates at an official `https://www.deeplearning.ai/the-batch/issue-*` URL. The subscription mailbox remains the optional final fallback. If all available paths fail, `ProviderUnavailableError` makes only The Batch ingestion attempt fail; other source attempts continue.

### 2026-10-30 — Phase 1, live-verification attempt and no-publication semantics

The Batch adapter now differentiates a valid empty Google News feed from a provider error. An empty feed produces a successful refresh with `fetched_count=0`, `persisted_count=0`, and `duplicate_count=0`, meaning no currently discoverable publication. An HTTP/DNS/rate-limit/invalid-feed error remains a failed attempt. This preserves the distinction between no new weekly issue, an existing issue that deduplicates on refresh, and a source failure.

### 2026-10-30 — Phase 2, Milestone 1: Telegram ↔ MCP connectivity

Added the independent `news_bot` package. It exposes only authorized `/start` and `/news` Telegram commands. `/news` opens an official MCP SDK `Client` against the configured Streamable HTTP URL, discovers `get_latest_news`, invokes it, and formats only the structured persisted records returned by MCP. It does not import `news_mcp`, provider adapters, or Phase 1 SQLite tables.

`python-telegram-bot` 22.x was added as the sole Milestone 1 dependency. Access fails closed when neither user IDs nor chat IDs are configured. A live Telegram call was not performed: dependency installation was blocked in the current execution environment by DNS, and no Telegram token was supplied.

During subsequent developer live startup, Telegram `getMe`, `deleteWebhook`, and polling initialization returned HTTP 200. The initial INFO-level `httpx` log emitted a Bot API URL, which embeds the token; this was treated as credential exposure. `news_bot.app.main()` now suppresses `httpx` and `httpcore` INFO logs. It also sets the official `run_polling(bootstrap_retries=3)` option because the library defaults to zero bootstrap retries; transient `getMe` timeouts now get three retries, while a persistent failure still stops clearly. The token must be revoked and regenerated through `@BotFather` before further testing.

### 2026-10-30 — Phase 2, Milestone 2: grounded LangChain + LangGraph agent

Added `news_bot.agent.McpNewsAgent` for ordinary, authorized Telegram text messages. It creates `ChatGroq` from `GROQ_API_KEY` and `GROQ_MODEL` (`openai/gpt-oss-20b`), opens LangChain's current `MCPAdapter` against the existing Streamable HTTP URL, and discovers the server tools through MCP. A minimal LangGraph `StateGraph` executes `model → ToolNode → model` with a recursion limit of eight steps.

The agent admits only read-only archive tools: `get_latest_news`, `search_news`, `get_article`, `get_related_news`, `get_news_by_date`, `get_digest_candidates`, and `list_sources`. `refresh_news` is intentionally unavailable to prevent a conversational request from silently causing external provider requests. The system prompt requires a tool result before factual news claims, calls the data an archive unless refreshed elsewhere, and requires publisher links and publication dates in its Sources section. As a final guard, when a tool output contains source URLs, the response is rejected unless it includes at least one retrieved URL.

### 2026-10-30 — Phase 2, Milestone 3: persistent memory and grounded follow-ups

Added two intentionally separate persistence mechanisms. `McpNewsAgent` now compiles the existing LangGraph state graph with an `AsyncSqliteSaver` checkpointer. Every request carries a `thread_id` in the form `telegram:{chat_id}:{user_id}`, preventing conversation history from crossing either Telegram boundary. The graph reads only the newest configurable number of messages (default 16, range 4–40) into each model call, while the checkpointer preserves restart continuity. The official `langgraph-checkpoint-sqlite` package is required at runtime; `LANGGRAPH_STRICT_MSGPACK=true` is documented for new checkpoint databases.

Added `news_bot.memory.DeliveryStore`, a separate bot-owned SQLite database. It stores chat ID, user ID, delivery date/time, delivery kind, ordered article IDs, and no article bodies or copied publisher metadata. A successful manual `/news` records its exact ordered IDs. `/history` or `/history YYYY-MM-DD` retrieves that user/chat's record and rereads each article through the existing MCP `get_article` tool. This preserves the Phase 1 ownership boundary.

Ordinary text now resolves clear ordinal references such as “the second article” against the caller's most recent saved delivery by appending its stable article ID to the grounded question. Article-aware MCP tool results and deterministic `/news`/`/history` output offer three inline buttons: explain, importance, and related archive coverage. Their callback data contains only the stable ID and enters the same user-specific LangGraph thread.

### 2026-10-30 — Phase 2, Milestone 4: scheduled daily digest

Added `news_bot.digest.DailyDigestService`, `/refresh`, and `/digest`. `/refresh` invokes only MCP `refresh_news` and returns source-level fetched/new/duplicate counts without exposing provider internals. Each digest first invokes the same MCP refresh operation, accepts partial provider success, reads a bounded latest-archive candidate list through MCP, then chooses at most `NEWS_BOT_DIGEST_LIMIT` recent records. Selection is deterministic: records must have a timezone-aware publication time within the prior 36 hours, must not have been delivered to that recipient before, and must not duplicate an already selected normalized title. The service never calls an LLM.

The `python-telegram-bot[job-queue]` extra supplies the official JobQueue/AsyncIOScheduler integration. Application startup registers one `run_daily` job per recipient at `time(06:00, tzinfo=Asia/Kolkata)`. The job queue is started and stopped with the Telegram application. `TELEGRAM_DIGEST_CHAT_IDS` selects explicit recipient chats; when unset, the union of authorized user and chat IDs is used. `/digest` calls the same implementation immediately for the requesting chat.

`scheduled_digest_runs` was added to the bot database with a recipient/date primary key, state, expiry lease, attempt counter, and safe truncated last error. `claim_digest` uses `BEGIN IMMEDIATE` so normal restarts and duplicate job execution cannot send an already completed date. A Telegram send is retried up to three times for `RetryAfter`, timeout, or network errors. Successful sends then atomically write the ordered `daily_digest` article references and mark the run `sent`; a valid zero-article outcome is marked `empty`. If refresh or send fails, the claim returns to `pending` for a later manual or operator retry. There remains an unavoidable external-message/database-commit crash window, documented as at-least-once behavior rather than falsely claiming exactly-once delivery.

### 2026-10-30 — Post-Milestone-4: Groq Browser Search fallback and response structure

Added an explicit web fallback without altering the independent MCP server. The normal LangGraph/MCP route remains first. For a named topic the agent prompt requires `search_news`; when its structured tool response has `data: []` (or an article lookup returns `data: null`), the agent invokes a second `ChatGroq` request configured with Groq's server-side `browser_search` tool, `tool_choice="required"`, and the existing configured `openai/gpt-oss-20b` model. An archive model answer that fails the existing retrieved-URL citation guard also falls through to Browser Search rather than exposing an internal availability error. MCP adapter/provider code and the Phase 1 database remain untouched.

Groq Browser Search is an external, live source and therefore `AgentAnswer.coverage` distinguishes `archive` from `web_search`. Telegram prepends a visible coverage label. The archive and web prompts both require Markdown `Coverage`, `Answer`, and `Sources` sections. Browser Search is incompatible with Groq API structured-output mode, so this is a presentation contract, not an incorrectly claimed JSON-schema guarantee. `NEWS_BOT_WEB_SEARCH_ENABLED` defaults to true and allows an operator to disable outbound live-web fallback.

The Telegram conversation handler logs the terminal exception type and root-cause message when an assistant request fails, while the user still receives only the generic availability response. This is intended for local diagnosis of MCP, Groq, or Browser Search failures and does not log credentials.

## Architecture and responsibilities

```text
MITNewsAdapter ─┐
TechCrunchAdapter ─┼─ provider-specific HTTP/RSS/API normalization
GuardianAdapter ──┤
BatchAdapter ─────┘
    ↓ RawArticle
LangGraph StateGraph
    Fetch → Normalize → Validate → Deduplicate → Persist
    ↓ NewsArticle
NewsArchive (SQLite + FTS5)
    ↓
MCPServer (tools and resource, Streamable HTTP)
```

Each adapter owns its own transport, parsing, and field extraction. It does not access SQLite. Guardian only calls the official Content API and requests `trailText`/byline metadata, not full article bodies. The Batch archive/Google requests use an explicit user agent and 15-second timeout; its IMAP transport runs in a worker thread so it does not block the asynchronous MCP server.

`IngestionService` builds the LangGraph `StateGraph`; it owns the prescribed ingestion stages and creates an ingestion-attempt record for every refresh. The graph returns an error state for normal provider unavailability, so a failed source does not crash a call that has other sources to process in a future milestone.

`NewsArchive` owns the persistence schema and async database operations. `server.py` wires the archive and adapters into the official MCP SDK. It contains no provider parsing or database SQL.

`news_bot` is a separate executable process. Its only news-data boundary is Streamable HTTP MCP. The deterministic `/news` handler uses `NewsMcpClient` and the official MCP SDK; `/history` calls `get_article` through the same boundary. The ordinary-text and callback handlers use `McpNewsAgent`, which discovers LangChain tools through `langchain.mcp.MCPAdapter`. The bot's own SQLite files contain only delivery references and conversation checkpoints. Neither path obtains a direct Phase 1 database connection or imports provider adapters. The command handlers format bounded Telegram messages and return generic availability messages for MCP, Groq API, graph, and local-memory failures without exposing internal details.

## Current folders

```text
src/news_mcp/
  adapters/mit_news.py  MIT-specific public RSS adapter
  adapters/techcrunch.py TechCrunch public RSS adapter
  adapters/guardian.py  Guardian Open Platform API adapter
  adapters/batch.py     The Batch authorized-feed/subscription-mailbox adapter
  archive.py            SQLite schema and archive operations
  config.py             Environment-backed settings
  models.py             Raw and normalized article contracts
  workflow.py           LangGraph ingestion workflow/service
  server.py             MCP construction and HTTP entry point
src/news_bot/
  config.py             Environment-backed Telegram/MCP authorization settings
  mcp_client.py         Official Streamable HTTP MCP archive client
  agent.py              Checkpointed LangGraph agent and MCPAdapter boundary
  memory.py             Bot-owned SQLite delivery-reference store
  digest.py             Refresh/select/send/record daily digest service
  app.py                Authorized commands, callbacks, and Telegram handler
  __main__.py           `python -m news_bot` entry point
tests/
  test_ingestion.py     Mocked provider ingestion behaviour
  test_archive_queries.py FTS5, ID, date, and digest archive behaviour
  test_mcp_server.py    MCP client discovery behaviour
  test_providers.py     Mocked TechCrunch, Guardian, and Batch configuration behaviour
  test_news_bot.py      Bot configuration and MCP client-boundary behaviour
docs/ENGINEERING.md     This technical record
```

No `data/` directory is committed: SQLite creates it at runtime only when the server or archive is used.

## Data contracts and persistence

`RawArticle` is provider output. `NewsArticle` is the normalized persisted contract:

- `article_id`: `art_` plus the first 24 hexadecimal SHA-256 characters of the canonical source URL; stable across refreshes.
- `title`, `source`, `author`, `category`, `description`, `source_url`.
- `published_at` and `fetched_at`: timezone-aware UTC datetimes.
- `keywords`, `original_summary`, and `content_status` (`excerpt_only` or `unavailable`).

The archive has four implemented tables:

| Table | Purpose |
| --- | --- |
| `sources` | Configured provider identity plus last status, check time, and error. |
| `articles` | Normalized archive. `article_id` is primary key; `source_url` is unique. |
| `ingestion_attempts` | Each successful or failed single-source refresh, including counts and error. |
| `article_duplicate_candidates` | Potential same-story pairs from different sources, similarity score, rule, and detection time. |

`articles_fts` is an FTS5 virtual table indexed with title, description, keywords, source, and an unindexed article ID. The persistence code keeps it synchronized after every insert/update. `search_news` tokenizes input into an FTS5 AND query, preventing FTS query syntax from being interpreted as SQL or arbitrary FTS operators.

URL-level deduplication is guaranteed: the canonical URL generates the stable ID and duplicate refreshes update rather than insert. Cross-source candidates are intentionally conservative: titles must be published within three days and have either at least 0.82 token-set overlap or 0.82 sequence similarity. Candidates are not silently merged; clients can inspect them through `get_related_news`.

## MCP contract implemented now

All tool responses use this envelope:

```json
{
  "data": "tool-specific value",
  "meta": {"schema_version": "1.0", "generated_at": "UTC ISO-8601"}
}
```

| Tool/resource | Arguments | Actual behaviour |
| --- | --- | --- |
| `list_sources()` | none | Lists each adapter registered in `sources`, including last refresh state. |
| `refresh_news(source=None)` | Optional configured source ID | Runs the LangGraph workflow, returning counts, failures, or configuration skips per selected source. |
| `get_latest_news(source=None, category=None, limit=10)` | Optional source/category; limit 1–100 | Retrieves stored records ordered by publication time. It never fetches news. |
| `search_news(query, source=None, from_date=None, to_date=None, limit=10)` | Query plus optional source and inclusive UTC date range | Searches FTS5-indexed persisted metadata/excerpts. Dates use `YYYY-MM-DD`. |
| `get_article(article_id)` | Stable article ID | Retrieves one stored article or `null` when absent. |
| `get_related_news(article_id)` | Stable article ID | Lists potential same-story articles from another source with similarity evidence. |
| `get_news_by_date(date, source=None)` | UTC date and optional source | Retrieves articles whose `published_at` falls on that date. |
| `get_digest_candidates(date, limit=10)` | UTC date, limit 1–100 | Retrieves persisted same-day stories for a future digest curator; it does not call an LLM. |
| `news://latest` | none | JSON representation of the ten newest persisted records. |
| `news://article/{article_id}` | Resource template | JSON representation of one stored article. |
| `news://archive/{date}` | Resource template | JSON representation of a UTC-dated archive. |

Tools return an error envelope for invalid source/limit values. The provider error itself is recorded as a failed ingestion attempt instead of being converted into fabricated articles.

For Phase 2 Milestone 1, `/news` calls the existing `get_latest_news(limit=...)` MCP tool. It deliberately returns the persisted archive, not a provider refresh, so it does not claim breaking-news freshness. The returned title, source, publication timestamp, permitted excerpt, and source URL are sent unchanged in meaning to Telegram.

For Phase 2 Milestones 2–3, ordinary Telegram text is supplied as a `HumanMessage` to the graph. The model receives the grounding system prompt and read-only MCP-derived LangChain tools. Tool results are appended by `ToolNode` and supplied to the model for its final answer. The graph ends only when the model emits an answer without tool calls. A recursion limit bounds malformed or repetitive tool-call loops. Milestone 3 compiles this graph with a SQLite checkpointer keyed by `telegram:{chat_id}:{user_id}` and passes only its bounded recent message window to the model.

The bot delivery database has three tables: `delivery_runs(delivery_id, chat_id, user_id, delivery_kind, delivery_date, delivered_at)`, `delivery_articles(delivery_id, article_position, article_id)`, and `scheduled_digest_runs(chat_id, user_id, digest_date, status, lease_expires_at, attempts, updated_at, last_error)`. The first two preserve ordered article references; the third is the restart-aware scheduling claim ledger. Article IDs are references only; `/history` resolves them via MCP `get_article`. A delivery is stored after `/news` succeeds in sending its archive messages. A `daily_digest` delivery is stored only after Telegram accepts its digest messages.

## Provider details and content policy

MIT News is configured for `https://news.mit.edu/topic/mitartificial-intelligence2-rss.xml`, the artificial-intelligence feed URL linked from MIT's own RSS directory. The adapter parses only feed entries and retains metadata plus the feed-supplied description after HTML is removed and the excerpt is capped at 2,000 characters. It does not crawl article pages.

TechCrunch uses the publisher's public `https://techcrunch.com/feed/` RSS endpoint and stores titles, links, categories/tags, and feed excerpts only. Guardian uses `https://content.guardianapis.com/search` with a required `GUARDIAN_API_KEY`, a single current-content request across technology, science, business, and world sections, 50 results maximum, and no full-body fields. The Guardian developer key is rate-limited; the adapter backs off on HTTP 429.

The Batch is a weekly newsletter with publicly accessible issue pages, but no documented official RSS/Atom/API interface was found. It can ingest from `DEEPLEARNING_AI_BATCH_RSS_URL` only when that is a publisher-authorized endpoint. Otherwise it first requests the public archive URL and uses only structured JSON-LD metadata containing a valid official issue URL, title, and `datePublished`. If the archive is blocked or lacks usable metadata, it requests the configured Google News search RSS. Google links are followed only as redirects and retained only after they resolve to a `deeplearning.ai/the-batch/issue-*` canonical URL. No issue page is fetched or parsed. A syntactically valid but empty Google feed is a successful zero-article refresh rather than a failure; candidate items that do not resolve to official issue URLs are failures because they cannot be trusted. The configured `DEEPLEARNING_AI_BATCH_IMAP_*` subscription inbox remains a final read-only fallback and reads at most ten messages.

## Reliability and recovery

- `httpx` requests use a 15-second timeout, explicit RSS `Accept` header, descriptive User-Agent, and redirect following.
- If MIT returns HTTP 403 to HTTPX, the MIT-only adapter retries the same official RSS URL with the local `curl` executable. This is a compatibility fallback for a public RSS feed that is accessible to curl but rejected by HTTPX at MIT's edge; it does not retrieve article pages, impersonate a browser, or change the declared User-Agent.
- MIT RSS retries network/HTTP/rate-limit failures up to three times with short exponential backoff.
- A response without a valid RSS version is rejected as malformed. A valid empty Google News RSS feed is the specific Batch no-new-publication case; individual malformed entries are logged and skipped.
- Each provider receives its own ingestion attempt. Sources disabled by missing configuration produce a recorded `skipped` attempt rather than failing other sources. Selected sources run independently, so a failed or rate-limited provider does not prevent the others from persisting articles.
- SQLite foreign keys, an article URL uniqueness constraint, primary keys, and FTS updates protect archive integrity.
- Invalid date ranges, invalid dates, empty FTS queries, unknown sources, and limits outside 1–100 return a structured tool error envelope rather than a database error.

## Security considerations

- `.env` and runtime SQLite archives are ignored by Git.
- No API credential, Telegram token, or user identifier is present in source control.
- `GUARDIAN_API_KEY` is read only from the environment and is sent only to Guardian's API as a request parameter.
- The Batch IMAP password is read only from the environment. Configure a limited app password instead of a primary mailbox credential, and grant the mailbox account only the access it needs.
- A Google News result cannot create a Batch article unless its redirect proves the final publisher URL matches the official issue pattern. A rejected redirect creates no archive record.
- The local default binds to `127.0.0.1`, not an externally reachable interface.
- The server intentionally returns original URLs and short provider excerpts, not copied article bodies.
- `news_bot` checks authorization before commands, text, and callbacks. Callback data is limited to a stable article ID validated against the Phase 1 ID format; it contains no publisher content or credential.
- Conversation checkpoint and delivery databases are separate from the MCP archive and must be persisted with restrictive filesystem permissions in deployment. `LANGGRAPH_STRICT_MSGPACK=true` is recommended for new checkpoint databases.
- The JobQueue is timezone-aware and schedules against `Asia/Kolkata`; it cannot run while the bot process is stopped or the host is asleep. The configured bot recipient IDs are sensitive identifiers and must remain only in `.env`.
- When enabled, Browser Search transmits the fallback question to Groq's server-side tool provider. It is visibly distinguished from archive material; operators can set `NEWS_BOT_WEB_SEARCH_ENABLED=false` where outbound web search is not appropriate.
- Authentication, TLS termination, and public deployment controls are not part of this local Milestone 1 server and must be addressed before remote exposure.

## Decisions and alternatives

| Decision | Reason | Alternative not selected |
| --- | --- | --- |
| MCP SDK v2 / `MCPServer` | The official SDK identifies v2 as the current stable line and supports Streamable HTTP directly. | Building new work on deprecated SSE. |
| Streamable HTTP | It is the current recommended HTTP transport and matches the future separate MCP client. | SSE, which current official docs mark as superseded. |
| `aiosqlite` | Keeps server/tool operations non-blocking while preserving SQLite's zero-admin local setup. | A synchronous connection in async tool handlers. |
| URL-derived ID | Stable and transparent refresh deduplication without an artificial surrogate that changes per ingest. | Random UUIDs. |
| LangGraph without LangChain | The ingest workflow is deterministic and needs no model, tool calling, or agent abstraction. | Adding a currently unused agent dependency. |
| FTS5 token search | Uses SQLite already present in the archive and keeps results grounded in persisted publisher metadata. | A vector database or LLM retrieval layer before either is required. |
| The Batch fallback chain | Uses structured publisher archive metadata first, then validates Google News redirects, while retaining the subscriber mailbox for deployments where public discovery fails. An explicitly authorized feed still takes precedence. | Issue-page/body scraping or a guessed WordPress feed endpoint. |
| Duplicate candidates, not auto-merges | Preserves source-specific coverage while surfacing likely same-story links. | Losing provenance by merging editorially distinct records. |
| Telegram via MCP client only | Preserves independent ownership: Phase 1 owns news and Phase 2 owns interaction. | Importing Phase 1 modules or querying its SQLite directly. |
| No LangChain/LLM in Phase 2 Milestone 1 | `/news` is deterministic MCP retrieval; adding an agent before natural-language routing would be unused complexity. | Premature agent and model integration. |
| `langchain[mcp]` `MCPAdapter` | The current LangChain MCP documentation recommends it; it connects URL targets using Streamable HTTP and discovers tools for LangChain. | Archived `langchain-mcp-adapters`, which current documentation says to migrate away from. |
| `ChatGroq` with `openai/gpt-oss-20b` | Explicit user-selected provider/model; Groq documents local function calling for this model. | Assuming a different provider or model. |
| Read-only tools in Milestone 2 | Prevents an ordinary conversation from triggering provider refreshes, while still serving archive and historical queries. | Letting the LLM call `refresh_news` implicitly. |
| SQLite LangGraph checkpoint | Provides restart-persistent, user/chat-isolated conversational state without adding a server dependency. | In-memory checkpoints, which lose restart continuity. |
| Separate delivery-reference database | Keeps Phase 1 as the single owner of article data while preserving exact delivery order. | Copying articles into the bot database or direct Phase 1 SQLite access. |
| Deterministic follow-up buttons | Gives contextual next actions without another LLM request or a second source of truth. | Generating ungrounded generic buttons. |
| PTB JobQueue daily trigger | Uses the installed Telegram application's async scheduler and timezone-aware `run_daily` API without a task queue or broker. | A separate queue/broker before multiple worker deployment is required. |
| SQLite date claim plus lease | Protects normal restarts/duplicate triggers while allowing a failed job to be retried. | An in-memory flag, which loses restart protection. |
| Refresh through MCP before selection | Keeps current-news ingestion owned by Phase 1 while allowing the bot to produce a fresh digest. | Importing provider adapters into `news_bot`. |
| Groq built-in Browser Search fallback | Uses the user-selected provider/model with no second search-provider credential, while keeping the archive route first. | Pretending archive misses are factual answers, or silently mixing live web coverage with archive records. |
| Markdown response structure for web fallback | Groq documents Browser Search as incompatible with structured outputs; prompts plus a Telegram coverage label preserve readable, honest formatting. | Claiming JSON-schema validation that Browser Search cannot provide. |

## Dependency record

`pyproject.toml` requires Python 3.11+ and current compatible major ranges for MCP, LangGraph, HTTPX, Feedparser, Pydantic, python-dotenv, aiosqlite, and `python-telegram-bot` 22.x. Milestone 2 adds `langchain[mcp]>=1.4.7,<2` and `langchain-groq>=1.1.3,<2`; the latter's current package metadata requires `langchain-core>=1.4.7,<2`. Milestone 3 adds `langgraph-checkpoint-sqlite>=3.1.2,<4` for `AsyncSqliteSaver`. Milestone 4 changes the Telegram dependency to `python-telegram-bot[job-queue]`, installing the official JobQueue scheduler dependency rather than adding a separate queue framework. Development dependencies are pytest and pytest-asyncio. `GROQ_API_KEY` and `GROQ_MODEL` are required runtime configuration and are never committed.

Official references checked before implementation:

- [MCP Python SDK: running a server](https://py.sdk.modelcontextprotocol.io/run/)
- [MCP Python SDK: Streamable HTTP ASGI app](https://py.sdk.modelcontextprotocol.io/run/asgi/)
- [MCP Python SDK v2 migration guide](https://py.sdk.modelcontextprotocol.io/migration/)
- [MCP Python SDK: client transports](https://py.sdk.modelcontextprotocol.io/client/transports/)
- [python-telegram-bot ApplicationBuilder v22.8](https://docs.python-telegram-bot.org/en/stable/telegram.ext.applicationbuilder.html)
- [python-telegram-bot JobQueue v22.8](https://docs.python-telegram-bot.org/en/stable/telegram.ext.jobqueue.html)
- [LangGraph package and documentation](https://pypi.org/project/langgraph/)
- [LangGraph persistence and SQLite checkpointer](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangChain MCPAdapter](https://docs.langchain.com/oss/python/langchain/mcp)
- [ChatGroq reference](https://reference.langchain.com/python/langchain-groq/chat_models/ChatGroq)
- [Groq GPT-OSS 20B model capability](https://console.groq.com/docs/model/openai/gpt-oss-20b)
- [Groq local tool calling](https://console.groq.com/docs/tool-use/local-tool-calling)
- [Groq Browser Search](https://console.groq.com/docs/tool-use/built-in-tools/browser-search)

## Testing strategy and verified results

The tests are designed to use `httpx.MockTransport`, never a live feed:

- `test_refresh_persists_normalized_articles_and_deduplicates`: valid mocked RSS → normalized records → a second refresh has zero inserts and two duplicates.
- `test_failed_provider_is_recorded_without_raising`: mocked connection error → failed attempt record, no archive rows.
- `test_mcp_client_discovers_milestone_one_tools_and_resource`: official in-process MCP client discovers the full current tool/resource surface and reads populated latest, article, and date resources.
- `test_mit_403_uses_curl_compatibility_fallback`: an HTTPX 403 causes the MIT-only curl fallback to provide a parseable RSS response.
- `test_archive_search_article_and_date_queries`: FTS search, stable-ID lookup, UTC-date filters, date range, and digest limit against a temporary SQLite archive.
- `test_archive_rejects_empty_fts_query`: invalid FTS input is rejected before it reaches SQLite.
- `test_archive_persists_across_reopen_and_honors_source_date_filters`: records survive a fresh archive instance and source/date filters select only the expected persisted records.
- `test_archive_identifies_similar_cross_source_titles`: persisted same-story candidates remain linked across publishers without merging records.
- `test_techcrunch_adapter_normalizes_public_rss` and `test_guardian_adapter_uses_official_api_response`: provider contracts work from mocked publisher payloads.
- `test_guardian_rate_limit_is_reported_as_provider_unavailable`, `test_techcrunch_timeout_is_reported_as_provider_unavailable`, and `test_techcrunch_invalid_response_is_reported_as_provider_unavailable`: rate limits, timeouts, and malformed non-RSS responses fail gracefully without records.
- `test_batch_public_archive_and_google_discovery_are_enabled_by_default`: configured public discovery transports are visible in source status.
- `test_batch_normalizes_official_subscription_email_without_scraping`: a mocked subscription email produces a normalized The Batch record without any web request.
- `test_batch_uses_public_archive_metadata_before_google_news`: valid publisher JSON-LD produces an article without invoking Google.
- `test_batch_uses_google_news_only_for_verified_official_issue_urls`: a blocked archive falls back to Google only when the redirect resolves to the official issue URL.
- `test_batch_reports_no_new_publication_for_an_empty_google_news_feed`: a blocked archive plus a valid empty Google feed produces a successful zero-article Batch refresh.
- `test_batch_marks_the_source_failed_when_public_transports_are_unavailable`: failed archive and Google HTTP requests produce a failed Batch attempt without raising out of the workflow.
- `test_all_phase_one_sources_are_registered_with_configuration_status`: all four source statuses are visible before a live refresh.
- `test_bot_settings_require_token_and_authorization` and `test_bot_settings_parse_authorized_ids_and_limit`: bot configuration requires a token plus an authorization boundary and validates IDs/limits.
- `test_news_mcp_client_discovers_and_calls_latest_news`: the Phase 2 client discovers and calls only `get_latest_news` through the MCP client interface.
- `test_news_mcp_client_retrieves_one_historical_article`: historical delivery rendering uses MCP `get_article`, not the Phase 1 SQLite file.
- `test_news_mcp_client_hides_connection_details`: MCP connection errors become a user-safe availability error.
- `test_telegram_conversation_runs_langgraph_agent_with_mcp_tools`: mocked authorized Telegram text traverses `McpNewsAgent`, the LangGraph model/tool loop, an MCPAdapter-shaped tool source, and a grounded response containing the returned publisher URL. The test proves that `/news`'s direct MCP client is not used by the conversational path.
- `test_agent_hides_model_and_mcp_failures`: mocked Groq rate-limit and MCP connection failures become one user-safe agent availability error.
- `test_agent_uses_a_user_specific_checkpoint_thread`: a persistent LangGraph thread carries one user/chat's prior message but does not leak it to another thread.
- `test_delivery_store_preserves_order_and_isolates_chat_and_user`: the bot's SQLite store preserves article position and rejects cross-user/chat history reads.
- `test_history_rereads_mcp_articles_and_ordinal_followup_uses_delivery_ids`: `/news` saves stable IDs, `/history` rereads them through the archive client, and an ordinal follow-up resolves its exact saved ID.
- `test_daily_digest_refreshes_filters_duplicates_and_prevents_restart_resend`: the digest refreshes via MCP-shaped client behavior, excludes previously delivered IDs, records a `daily_digest`, and rejects a second same-date delivery after restart-equivalent reentry.
- `test_failed_digest_is_not_marked_as_delivered_and_can_be_retried`: an unavailable archive releases the claim rather than claiming success; a later healthy attempt sends and records the digest.
- `test_refresh_command_uses_the_mcp_archive_client`: authorized `/refresh` calls only the MCP-shaped archive client and returns its structured source result.
- `test_empty_archive_search_uses_labeled_groq_browser_fallback`: an empty `search_news` tool response triggers the injected browser-search model and labels the answer as live web coverage.
- `test_uncited_archive_response_uses_groq_browser_fallback`: an archive-model answer that omits a retrieved source URL safely falls through to the injected browser-search model.

**Executed on 2026-10-29:** `python3 -m compileall -q src tests`, a Pydantic-only normalized article/UTC serialization contract probe, and TOML parsing of `pyproject.toml` — all passed. Python reported `3.12.6`; installed global versions were HTTPX `0.28.1` and Pydantic `2.11.10`.

**Executed in the project virtual environment on 2026-10-29:** `.venv/bin/pytest -q` — **4 passed in 0.78s**. `pythonpath = ["src"]` is included in pytest configuration, so the standard `pytest -q` command resolves the source-layout package without requiring an editable install.

**Live MCP verification supplied by the developer on 2026-10-29:** MCP Inspector successfully discovered the initial tools and invoked `list_sources` with `isError: false`. A real `refresh_news(source="mit-news")` then returned `status: success`, `fetched_count: 50`, `persisted_count: 50`, and `duplicate_count: 0`. Server logs showed HTTPX receiving MIT's `403` followed by the successful compatibility fallback, proving real RSS retrieval, normalization, LangGraph ingestion, SQLite persistence, and MCP response serialization end-to-end. The ingestion/fallback suite verifies the compatibility branch with a mocked HTTPX 403.

**Environment note:** `python3 -m pytest -q` from the system Python exited with `No module named pytest`; tests are now verified through the project virtual environment instead.

**Executed after Milestone 2 on 2026-10-29:** `.venv/bin/pytest -q` — **6 passed in 0.49s**; `python3 -m compileall -q src tests` and `git diff --check` — passed. A read-only FTS query against the live SQLite archive returned two real MIT records for `infrastructure`, headed by `art_ba816ae2c66dda0a8ef1163b`.

**Executed after The Batch subscription-mailbox change on 2026-10-30:** `.venv/bin/pytest -q` — **13 passed in 0.59s**; `python3 -m compileall -q src tests` and `git diff --check` — passed. All provider payloads and subscription emails are mocked in this suite; no live Guardian request or mailbox login was performed.

**Executed after public archive/Google fallback implementation on 2026-10-30:** `.venv/bin/pytest -q` — **16 passed in 0.61s**; `python3 -m compileall -q src tests` and `git diff --check` passed. The controlled environment could not resolve `www.deeplearning.ai` through `curl`; the web fetch received HTTP 403. This was recorded as an access limitation, not bypassed. Mocked tests cover the archive-success, archive-to-Google, and all-paths-unavailable cases. A live deployment refresh remains required to determine current publisher/Google availability.

**Live-verification attempt on 2026-10-30:** the current Codex execution environment denied binding `127.0.0.1:8000` (`operation not permitted`) and could not resolve any provider hostname (`[Errno 8] nodename nor servname provided, or not known`). Direct configured-adapter calls for MIT News, Guardian, TechCrunch, and The Batch all therefore failed before reaching their providers. In-process official MCP-client `refresh_news` calls returned four independent `status=failed`, zero-count results with `is_error=false`; the all-source call returned all four results, confirming one source does not abort the workflow. The Batch failure records show archive metadata was attempted before Google News; IMAP was not configured. No live Google item was available to resolve and verify.

**Persisted archive verification on 2026-10-30:** SQLite contained 50 real MIT News records created by the developer's earlier live Inspector refresh. In-process MCP `get_latest_news(source="mit-news", limit=5)` returned five records with `is_error=false`, and `search_news(query="infrastructure", source="mit-news")` returned two records with `is_error=false`. No live successful refresh was possible in this environment, so live repeat-refresh deduplication and live Guardian/TechCrunch/Batch ingestion remain unverified. The automated tests cover deduplication with mocked publisher data. After the no-publication change, `.venv/bin/pytest -q` passed **17 tests in 0.66s**; compilation and diff checks passed.

**Live MCP verification supplied by the developer on macOS on 2026-10-29:** `list_sources` discovered all four enabled providers with no initial status/error. A Streamable HTTP MCP `refresh_news` call then returned `isError: false` and independent per-source outcomes: MIT News fetched/persisted **50/50**, TechCrunch **20/20**, and The Guardian **50/50** real records. The Batch fetched/persisted **0/0** and failed with `archive did not expose valid JSON-LD issue metadata` followed by `Google News had no verified official Batch issue URLs`. This failure did not prevent the other three successful provider ingestions.

The developer refreshed MIT News, The Guardian, and TechCrunch individually a second time. Results were respectively **50 fetched, 0 persisted, 50 duplicates**; **50/0/50**; and **20/0/20**. This is live URL-based deduplication verification. A repeated Batch refresh returned the same isolated unavailable result. `get_latest_news(source="mit-news", limit=5)` returned five persisted real MIT records. `search_news(query="artificial intelligence", limit=10)` returned real normalized records from MIT News and The Guardian, including a Guardian article fetched during the live run. `.venv/bin/pytest -q` completed with **17 passed in 0.63s**; `python3 -m compileall -q src tests` completed successfully.

**Final technical review on 2026-10-30:** added focused automated verification for SQLite reopening with source/date filters, populated MCP resources, Guardian HTTP 429, TechCrunch timeout, and malformed non-RSS responses. The malformed-response test found and corrected a bug that had treated HTML without an RSS version as an empty successful feed. The full suite then passed: `.venv/bin/pytest -q` — **21 passed in 0.65s**; `python3 -m compileall -q src tests` and `git diff --check` passed.

**Phase 2 Milestone 1 verification on 2026-10-30:** the initial implementation passed `.venv/bin/pytest -q` — **25 passed in 0.61s**. After adding finite Telegram bootstrap retries for the observed timeout, it passed again — **25 passed in 0.66s**; `python3 -m compileall -q src tests` and `git diff --check` passed. The tests cover the bot configuration and MCP boundary with fakes. Dependency installation could not be completed in the Codex execution environment because DNS prevented pip from fetching build requirements. On the developer's Mac, the installed Telegram process successfully received HTTP 200 from `getMe`, `deleteWebhook`, and `getUpdates`, proving token authentication and polling startup. The developer then verified `get_latest_news(limit=5)` through MCP Inspector against the same server; it returned five persisted MIT News records. Authorized Telegram `/news` delivery subsequently established the live Telegram → MCP → SQLite archive → Telegram path, and `/start` was also manually live-verified.

**Phase 2 Milestone 2 verification on 2026-10-30:** `.venv/bin/pytest -q` first passed **26 tests in 1.57s**, passed again in **1.24s**, and completed after model/MCP-failure coverage — **28 passed in 0.92s** and later **0.77s**; `python3 -m compileall -q src tests` and `git diff --check` passed. The mocked end-to-end test exercises Telegram message handling, LangGraph routing, a model tool call, a `ToolNode` invocation, and a publisher URL in the response. The failure coverage checks safe handling of a simulated Groq rate limit and an unavailable MCP server. These tests do not make a Groq request or connect to the live MCP HTTP endpoint. An attempted `.venv/bin/python -m pip install -e '.[dev]'` failed before dependency resolution because this execution environment could not resolve PyPI's `setuptools` endpoint (`nodename nor servname provided`). Therefore `langchain`, `langchain-groq`, and the `langchain[mcp]` extra were not import-verified here, and live Groq function calling remains pending a network-enabled local run with a valid `GROQ_API_KEY`.

**Phase 2 Milestone 3 verification on 2026-10-30:** `.venv/bin/pytest -q` passed **32 tests in 0.82s**; `python3 -m compileall -q src tests` and `git diff --check` passed. The installed environment does not contain `langgraph.checkpoint.sqlite`; importing `AsyncSqliteSaver` therefore correctly reports the user-safe installation error until `pip install -e '.[dev]'` completes in a network-enabled environment. Milestone 3 unit tests inject LangGraph's built-in `MemorySaver` to verify thread-key persistence and isolation without simulating a SQLite checkpointer. The delivery-reference store itself uses the already-installed `aiosqlite` and is exercised on temporary real SQLite files. No live Telegram, Groq, MCPAdapter, or new SQLite-checkpointer run is claimed here.

**Phase 2 Milestone 4 verification on 2026-10-30:** `.venv/bin/pytest -q` passed **35 tests in 0.83s**; `python3 -m compileall -q src tests` and `git diff --check` passed. The tests use a real temporary bot SQLite file, an MCP-shaped fake archive, and a fake Telegram sender. They verify refresh-before-selection, prior-delivery exclusion, exact-date duplicate prevention, `daily_digest` ID persistence, failed-refresh retryability, the 36-hour selection rule, and the authorized MCP-only `/refresh` command. The installed environment exposes `telegram.ext.JobQueue`, but no live JobQueue firing at 06:00, Telegram send, MCP refresh, or provider refresh was performed during this milestone. Those require the developer's always-on bot and MCP processes.

**Browser Search extension verification on 2026-10-30:** `.venv/bin/pytest -q` passed **37 tests in 1.05s**; `python3 -m compileall -q src tests` and `git diff --check` passed. Unit coverage injects a fake browser model after a real LangGraph `ToolNode` returns an empty `search_news` response or an uncited archive answer; it verifies the fallback's live-web coverage label and a supplied source URL. No real Groq Browser Search request is claimed until the developer runs it with the configured `GROQ_API_KEY`.

## Known limitations and next milestone

The Guardian key and TechCrunch RSS now have successful macOS live verification. The Batch public archive currently does not expose the required issue JSON-LD metadata, and Google News discovery currently produced no redirect that could be verified as an official issue URL. The optional subscribed IMAP inbox needs the actual sender address and an app password. FTS search is lexical, not semantic, and daily digest selection is deterministic rather than LLM-curated. Groq Browser Search citation formatting is provider-generated and cannot use Groq structured output; it must be live-verified before production reliance. Exact once delivery is not possible across Telegram's external API and local SQLite: a process crash after Telegram accepts a message but before the local transaction commits can produce an at-least-once retry scenario.

## Phase 1 acceptance review

| Acceptance criterion | Status | Verification |
| --- | --- | --- |
| Server starts independently; MCP client discovers/invokes tools | Pass | Developer ran the Streamable HTTP server and Inspector; automated MCP client discovers all eight tools and three resource patterns. |
| Real available-source ingestion | Pass for three providers | Live macOS refresh persisted 50 MIT, 20 TechCrunch, and 50 Guardian articles. |
| Original four-source objective | **Partial** | The Batch has no verified public metadata/API/feed result and Google discovery yielded no verifiable official issue URL. |
| One normalized schema and persistent archive | Pass | All live records use the normalized response schema; SQLite reopen, stable IDs, and FTS are covered by tests. |
| Historical retrieval, source/date filters, resources | Pass | Archive reopen/filter test and populated MCP latest/article/date-resource test pass; live `get_latest_news` and FTS search returned real stored records. |
| Refresh deduplication | Pass for three providers | Live second refresh produced zero inserts and 50/50/20 duplicates. |
| Independent failures and accurate statuses | Pass | Batch produced `failed` with its two factual acquisition errors while MIT, Guardian, and TechCrunch completed successfully. A valid empty Google feed is separately tested as zero-article `success`, never confused with this Batch failure. |
| Timeout, rate limit, invalid response handling | Pass in automated tests | HTTP timeout, Guardian 429, malformed RSS/HTML, provider connection failure, and Batch public-transport failure are safely classified. |
| Tests and documentation | Pass | 21 tests pass; this record contains both live and mocked verification evidence. |

**Sign-off decision:** Phase 1 is technically ready for MIT News, The Guardian, and TechCrunch, but it is **not fully signed off against the original four-source requirement**. The remaining Phase 1 decision is whether to configure the optional official Batch subscription mailbox or formally accept the provider as unavailable until DeepLearning.AI exposes a permitted working interface.

## Phase 2 status, blockers, and boundary

Phase 2 Milestones 1–4 are authorized and implemented. Milestone 1 has live verification for startup, authorization, `/start`, and `/news` delivery through MCP. The developer has live-verified Milestone 3 `/history`, suggested follow-ups, and contextual follow-up answers. The post-Milestone-4 Browser Search extension has mocked fallback coverage only. Live Groq function calling, Browser Search, LangChain MCPAdapter discovery, SQLite checkpoint persistence, and a 06:00 scheduled delivery remain pending local end-to-end verification.

Milestone 5 has not started: production deployment, restart recovery exercises, operational monitoring, final security review, and end-to-end acceptance verification remain. Before production, resolve or explicitly accept the partially unmet Batch requirement, deploy both independent processes on always-on infrastructure with durable bot/MCP SQLite volumes, set explicit digest recipient IDs, and verify a controlled manual `/digest` before relying on the first scheduled send. The Telegram application must remain a separately deployed MCP client; it must not import provider adapters, query the Phase 1 database, or become the MCP server.
