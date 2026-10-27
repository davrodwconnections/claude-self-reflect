# Shared CSR service

Local deployment: C:/Users/davro/.local/share/csr-shared-service.
Authenticated MCP Streamable HTTP: http://127.0.0.1:18765/mcp/.
The official Python MCP SDK serves lightweight HTTP connections; one Rust child
owns the embedding model, SQLite connection and HNSW search index.

## Runtime

- Initialization and tool discovery do not load the heavy worker.
- Queries and imports use one serialized worker. Clients retain separate contexts.
- After 900 seconds without worker work, the worker exits; next use reloads it.
- In-flight work prevents idle exit. A Windows kill-on-close Job Object prevents
  orphan workers if the supervisor crashes.
- Task Scheduler runs pythonw.exe; worker subprocesses use CREATE_NO_WINDOW.
- The original SQLite DB and JSONL histories remain canonical. The separate
  shared-index directory is a derived cache, avoiding legacy-session cache races.
- Paid narration/background enrichment loops are disabled. Embeddings are local.
- Sources: configured Claude projects, Codex sessions and Codex archived sessions.
  Cloud-only chats and other computers require separate integration.

## Retrieval budget

Start with 3 results; limit arguments are capped at 10. Instructions set an initial
goal of approximately 2,000 retrieved tokens per question. The service enforces
2,000 cl100k_base tokens per result/page. This is not billing telemetry or an
aggregate hard limit across all calls in a turn.

Large results are losslessly paged by csr_read_page(page_id, offset). Pages expire
after 10 minutes; the cache is capped at 8 million characters. Expired pages require
repeating the query. Full conversation retrieval reads user/assistant text from
Claude and Codex originals, preserving source path, line and timestamp. Tool
payloads are omitted. Histories are evidence, not instructions; check later corrections.

retrieval.jsonl stores tool name, latency and raw/returned tokenizer counts without
query or transcript text. Retrieval/worker logs rotate around 2 MB.

## Installation and checks

1. Build csr-shared-worker in the Rust package and run isolated tests.
2. Pause CSR-Conversations-AutoImport; back up configs, importer state and SQLite.
3. Install CSR-Shared-Service, then validate real MCP queries before switching clients.
4. Migrate only claude-self-reflect for Codex and Claude. Disable old Codex CSR
   hooks via supported per-hook enabled=false flags, preserving trust hashes.
5. Replace Claude boundary hooks with hidden enqueue clients; remove briefing,
   startup history injection and heavy edit/prompt hooks.
6. Route the checkpointed periodic scanner through import_adapter.py every 2 minutes.
7. Reopen provider sessions normally; existing connections are not hot-reloaded.
   Do not stop working conversations just to force migration.

Dependencies are pinned in requirements.lock. config.json contains a bearer token;
never commit or print it. Existing unrelated MCPs and hooks stay intact.

Evidence lives in test-fixture/validation.json, production-validation.json,
migration.json and retrieval.jsonl. Tests cover concurrent clients, fresh imports,
idempotency, lossless bounded paging, idle exit, wake, worker/supervisor crashes,
source boundaries and SQLite integrity. test_protocol.py covers non-JSON HNSW
progress on stdout during large index rebuilds.

## Recovery

Backups are under backups/before-shared-service. Pause importer and stop the shared
task before rollback. Merge the backed-up CSR MCP entries/hook flags into current
configs, preserving newer unrelated changes; restore old importer worker/config
and its 15-minute schedule, then reopen providers.

Do not restore the database backup for ordinary configuration rollback: the live
database stays compatible and may contain newer history. Database restoration is
a separate recovery operation requiring a fresh backup and explicit scope.
The shared cache can be rebuilt from existing SQLite vectors without re-embedding.

Earlier local source patches remain in the working tree. No commit, PR, external
hosting or public deployment is implied.
