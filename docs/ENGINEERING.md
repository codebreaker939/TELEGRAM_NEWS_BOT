# Engineering record

## Scope and implementation history

### 2026-10-29 — Phase 1, Milestone 1: archive and MIT News vertical slice

Implemented the initial independent MCP-server vertical slice. It handles a public MIT News artificial-intelligence topic RSS feed, stores only publisher metadata and a stripped feed excerpt, and exposes the persisted archive over Streamable HTTP MCP.

This is not Phase 2: no Telegram code, LLM provider, LangChain agent, user memory, delivery records, or `langchain-mcp-adapters` have been added.

## Architecture and responsibilities

```text
MITNewsAdapter (HTTP + RSS parsing)
    ↓ RawArticle
LangGraph StateGraph
    Fetch → Normalize → Validate → Deduplicate → Persist
    ↓ NewsArticle
NewsArchive (SQLite + FTS5)
    ↓
MCPServer (tools and resource, Streamable HTTP)
```

`MITNewsAdapter` owns HTTP timeouts, retry/backoff, rate-limit detection, feed parsing, and source-specific field extraction. It does not access SQLite.

`IngestionService` builds the LangGraph `StateGraph`; it owns the prescribed ingestion stages and creates an ingestion-attempt record for every refresh. The graph returns an error state for normal provider unavailability, so a failed source does not crash a call that has other sources to process in a future milestone.

`NewsArchive` owns the persistence schema and async database operations. `server.py` wires the archive and adapters into the official MCP SDK. It contains no provider parsing or database SQL.

## Current folders

```text
src/news_mcp/
  adapters/mit_news.py  MIT-specific public RSS adapter
  archive.py            SQLite schema and archive operations
  config.py             Environment-backed settings
  models.py             Raw and normalized article contracts
  workflow.py           LangGraph ingestion workflow/service
  server.py             MCP construction and HTTP entry point
tests/
  test_ingestion.py     Mocked provider ingestion behaviour
  test_mcp_server.py    MCP client discovery behaviour
docs/ENGINEERING.md     This technical record
```

No `data/` directory is committed: SQLite creates it at runtime only when the server or archive is used.

## Data contracts and persistence

`RawArticle` is provider output. `NewsArticle` is the normalized persisted contract:

- `article_id`: `art_` plus the first 24 hexadecimal SHA-256 characters of the canonical source URL; stable across refreshes.
- `title`, `source`, `author`, `category`, `description`, `source_url`.
- `published_at` and `fetched_at`: timezone-aware UTC datetimes.
- `keywords`, `original_summary`, and `content_status` (`excerpt_only` or `unavailable`).

The archive has three implemented tables:

| Table | Purpose |
| --- | --- |
| `sources` | Configured provider identity plus last status, check time, and error. |
| `articles` | Normalized archive. `article_id` is primary key; `source_url` is unique. |
| `ingestion_attempts` | Each successful or failed single-source refresh, including counts and error. |

`articles_fts` is an FTS5 virtual table indexed with title, description, keywords, source, and an unindexed article ID. The persistence code keeps it synchronized after every insert/update. It is provisioned now because it is part of archive correctness, but FTS queries are intentionally deferred to the next milestone.

The current deduplication guarantees are URL-level: the canonical URL generates the stable ID and duplicate refreshes update the stored record rather than insert a second one. Cross-source story similarity is not implemented yet; it needs an explicit, reviewable rule in a later milestone rather than a speculative heuristic.

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
| `refresh_news(source=None)` | Optional `mit-news` | Runs the LangGraph workflow, returning counts and failures per selected source. |
| `get_latest_news(source=None, category=None, limit=10)` | Optional source/category; limit 1–100 | Retrieves stored records ordered by publication time. It never fetches news. |
| `news://latest` | none | JSON representation of the ten newest persisted records. |

Tools return an error envelope for invalid source/limit values. The provider error itself is recorded as a failed ingestion attempt instead of being converted into fabricated articles.

## Provider details and content policy

MIT News is configured for `https://news.mit.edu/topic/mitartificial-intelligence2-rss.xml`, the artificial-intelligence feed URL linked from MIT's own RSS directory. The adapter parses only feed entries and retains metadata plus the feed-supplied description after HTML is removed and the excerpt is capped at 2,000 characters. It does not crawl article pages.

DeepLearning.AI's The Batch has been investigated but is not implemented: its own site describes it as a weekly newsletter, and no official RSS endpoint is assumed. Guardian, TechCrunch, and The Batch will be separate adapters, with Guardian requiring an environment API key and access/terms verification before code is added.

## Reliability and recovery

- `httpx` requests use a 15-second timeout, explicit RSS `Accept` header, descriptive User-Agent, and redirect following.
- If MIT returns HTTP 403 to HTTPX, the MIT-only adapter retries the same official RSS URL with the local `curl` executable. This is a compatibility fallback for a public RSS feed that is accessible to curl but rejected by HTTPX at MIT's edge; it does not retrieve article pages, impersonate a browser, or change the declared User-Agent.
- MIT RSS retries network/HTTP/rate-limit failures up to three times with short exponential backoff.
- A malformed feed with no entries fails the ingestion attempt. Individual malformed entries are logged and skipped.
- Each provider receives its own ingestion attempt. The service already iterates selected providers independently; later adapter additions therefore preserve partial success.
- SQLite foreign keys, an article URL uniqueness constraint, primary keys, and FTS updates protect archive integrity.

## Security considerations

- `.env` and runtime SQLite archives are ignored by Git.
- No API credential, Telegram token, or user identifier is present in source control.
- The local default binds to `127.0.0.1`, not an externally reachable interface.
- The server intentionally returns original URLs and short provider excerpts, not copied article bodies.
- Authentication, TLS termination, and public deployment controls are not part of this local Milestone 1 server and must be addressed before remote exposure.

## Decisions and alternatives

| Decision | Reason | Alternative not selected |
| --- | --- | --- |
| MCP SDK v2 / `MCPServer` | The official SDK identifies v2 as the current stable line and supports Streamable HTTP directly. | Building new work on deprecated SSE. |
| Streamable HTTP | It is the current recommended HTTP transport and matches the future separate MCP client. | SSE, which current official docs mark as superseded. |
| `aiosqlite` | Keeps server/tool operations non-blocking while preserving SQLite's zero-admin local setup. | A synchronous connection in async tool handlers. |
| URL-derived ID | Stable and transparent refresh deduplication without an artificial surrogate that changes per ingest. | Random UUIDs. |
| LangGraph without LangChain | The ingest workflow is deterministic and needs no model, tool calling, or agent abstraction. | Adding a currently unused agent dependency. |

## Dependency record

`pyproject.toml` requires Python 3.11+ and current compatible major ranges for MCP, LangGraph, HTTPX, Feedparser, Pydantic, python-dotenv, and aiosqlite. Development dependencies are pytest and pytest-asyncio. LangChain is intentionally deferred to authorized Phase 2 because this milestone contains no LLM or agent functionality.

Official references checked before implementation:

- [MCP Python SDK: running a server](https://py.sdk.modelcontextprotocol.io/run/)
- [MCP Python SDK: Streamable HTTP ASGI app](https://py.sdk.modelcontextprotocol.io/run/asgi/)
- [MCP Python SDK v2 migration guide](https://py.sdk.modelcontextprotocol.io/migration/)
- [LangGraph package and documentation](https://pypi.org/project/langgraph/)
- [LangChain package and documentation](https://pypi.org/project/langchain/)

## Testing strategy and verified results

The tests are designed to use `httpx.MockTransport`, never a live feed:

- `test_refresh_persists_normalized_articles_and_deduplicates`: valid mocked RSS → normalized records → a second refresh has zero inserts and two duplicates.
- `test_failed_provider_is_recorded_without_raising`: mocked connection error → failed attempt record, no archive rows.
- `test_mcp_client_discovers_milestone_one_tools_and_resource`: official in-process MCP client discovers three tools and `news://latest`.
- `test_mit_403_uses_curl_compatibility_fallback`: an HTTPX 403 causes the MIT-only curl fallback to provide a parseable RSS response.

**Executed on 2026-10-29:** `python3 -m compileall -q src tests`, a Pydantic-only normalized article/UTC serialization contract probe, and TOML parsing of `pyproject.toml` — all passed. Python reported `3.12.6`; installed global versions were HTTPX `0.28.1` and Pydantic `2.11.10`.

**Executed in the project virtual environment on 2026-10-29:** `.venv/bin/pytest -q` — **4 passed in 0.78s**. `pythonpath = ["src"]` is included in pytest configuration, so the standard `pytest -q` command resolves the source-layout package without requiring an editable install.

**Live MCP verification supplied by the developer on 2026-10-29:** MCP Inspector successfully discovered all three tools and invoked `list_sources` with `isError: false`. A real `refresh_news(source="mit-news")` then returned `status: success`, `fetched_count: 50`, `persisted_count: 50`, and `duplicate_count: 0`. Server logs showed HTTPX receiving MIT's `403` followed by the successful compatibility fallback, proving real RSS retrieval, normalization, LangGraph ingestion, SQLite persistence, and MCP response serialization end-to-end. The four-test suite additionally verifies the fallback branch with a mocked HTTPX 403.

**Environment note:** `python3 -m pytest -q` from the system Python exited with `No module named pytest`; tests are now verified through the project virtual environment instead.

## Known limitations and next milestone

Milestone 1 has one live-source adapter only and exposes only three tools. It does not yet implement FTS search, article-ID retrieval, date queries, digest candidates, dynamic article/date resources, all-source concurrency, cross-source near-duplicate detection, Guardian API credentials/rate policies, or MCP Inspector verification.

**Recommended next milestone:** complete the archive retrieval surface (`search_news`, `get_article`, `get_news_by_date`, `get_digest_candidates`) using the already populated normalized archive and FTS5, then add its mocked tests and MCP resource templates. This is still Phase 1 and needs no Telegram or LLM work.

Phase 2 architectural boundary: Telegram must remain a separately deployed, authenticated MCP client using `langchain-mcp-adapters`; it must not import these provider adapters or turn into the MCP server. It will add LangChain agent tools, LangGraph conversation state/checkpointing, user-scoped memory, and delivery records only when authorized.
