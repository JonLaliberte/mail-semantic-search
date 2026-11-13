# Configuration

All settings come from environment variables, normally set in a `.env` file in the project folder. Start from `.env.example`. `.env` is read from the current working directory, so run commands from the project folder.

Only `EMAIL_DIR` usually needs changing. Everything else has a sensible default.

## Core

| Variable | Default | Description |
|---|---|---|
| `EMAIL_DIR` | `~/Library/Application Support/MailMate/Messages` | Folder containing your `.eml` files. Use an absolute path in `.env`; Docker needs it to mount the folder. Symlinks are followed. |
| `EMBEDDING_MODEL` | `BGE-base-en-v1.5` | Embedding model. Options: `BGE-base-en-v1.5` (best quality), `BGE-small-en-v1.5`, `nomic-embed-text-v1`, `all-MiniLM-L6-v2`. Changing it requires a full re-index; see [Switching models](#switching-models). |
| `BATCH_SIZE` | `32` | Emails embedded per batch (1–500). Lower it if indexing runs out of memory. |
| `SEARCH_RESULTS` | `10` | Results returned by `search` (1–10000). |

## Storage

| Variable | Default | Description |
|---|---|---|
| `DATA_VOLUME_PATH` | `./data` | Docker only: host folder mounted as `/app/data` inside the container. Set to an absolute path to keep the index on an external drive, e.g. `/Volumes/My Drive/mail-semantic-search/data`. |
| `CHROMADB_PATH` | `./data/chromadb` | Vector store location. |
| `DATABASE_PATH` | `./data/database.db` | SQLite metadata database. |
| `MODEL_CACHE_DIR` | `./data/models` | Downloaded models. Models load from here first and only hit the network when missing. |
| `STAGING_DIR` | `~/Documents/mailmate-staged` | Where `stage` / `stage_email_attachments` copy an email's attachments and `.eml` so sandboxed AI clients can read them. |

Docker's `docker-compose.yml` sets `CHROMADB_PATH`, `MODEL_CACHE_DIR` and `DATABASE_PATH` to paths under `/app/data`, so with Docker you only set `DATA_VOLUME_PATH`.

## Search quality

Both of these are off by default and run entirely locally.

| Variable | Default | Description |
|---|---|---|
| `RERANK_ENABLED` | `false` | Re-rank the top candidates with a cross-encoder. More precise results for a little extra latency. Override per query with `--rerank/--no-rerank`. |
| `RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder model. |
| `RERANK_MAX_CANDIDATES` | `50` | Candidate pool size before re-ranking. |
| `RERANK_MAX_TEXT_CHARS` | `1200` | Max characters of each candidate passed to the reranker. |
| `QUERY_PARSER_ENABLED` | `false` | Use a local LLM to turn natural-language queries ("from Sam last March with a PDF") into filters. Override per query with `--auto-filters/--no-auto-filters`. |
| `QUERY_PARSER_ENDPOINT` | `http://localhost:11434/api/generate` | Parser endpoint (Ollama's generate API by default). |
| `QUERY_PARSER_MODEL` | `llama3.1:8b` | Parser model name. |
| `QUERY_PARSER_TIMEOUT_SECONDS` | `8` | Parser timeout. On timeout, search runs without extracted filters. |

## Indexing

| Variable | Default | Description |
|---|---|---|
| `INCREMENTAL_OVERLAP_SECONDS` | `86400` | Re-scan window subtracted from the incremental watermark, to catch clock skew and late file writes. |
| `BODY_PREVIEW_LIMIT` | `5000` | Max characters stored as the body preview. The embedded text is capped separately by the model's context window (~2000 characters for BGE-base), so this only affects snippet display. |
| `MAX_EMAIL_FILE_SIZE` | `52428800` (50 MB) | Larger `.eml` files are skipped. |
| `MAX_ATTACHMENT_SIZE` | `10485760` (10 MB) | Larger attachments are not text-extracted. |
| `MAX_ATTACHMENT_TEXT_PER_FILE` | `2000` | Characters of extracted text kept per attachment. |
| `MAX_TOTAL_ATTACHMENT_TEXT` | `5000` | Characters of attachment text kept per email. |
| `QUOTE_STRIP_ENABLED` | `true` | Remove quoted replies before embedding, so each message is indexed by what it actually says. |
| `QUOTE_STRIP_TIMEOUT_SECONDS` | `2.0` | Per-message time limit for quote stripping. |
| `QUOTE_STRIP_MAX_CHARS` / `QUOTE_STRIP_MAX_LINES` | `50000` / `1500` | Messages above either limit skip quote stripping. |
| `INDEX_RUNTIME_DIAGNOSTICS` | `true` | Log heartbeats and stall dumps during indexing. |
| `INDEX_HEARTBEAT_SECONDS` | `60` | Heartbeat interval. |
| `INDEX_STALL_DUMP_SECONDS` | `300` | Dump thread stacks to the log if indexing makes no progress for this long. |

## Queries and results

| Variable | Default | Description |
|---|---|---|
| `MAX_FILTERED_SEARCH_LIMIT` | `1000` | Cap on metadata-only results from `query` / `query_emails`. |
| `MAILMATE_LINK_SCHEME` | `mid` | Scheme for the pre-built `markdown_link` on results: `mid` or `message`. `mid_url` and `message_url` are always included. `mid` is the default because Apple Mail claims `message:` on macOS, so `mid:` reaches MailMate more reliably. Unknown values log a warning and fall back to `mid`. |

## MCP server

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` (the client starts the server) or `http` (standalone server; see [MCP over HTTP](mcp.md#mcp-over-http-full-disk-access-workaround)). |
| `MCP_HOST` | `127.0.0.1` | HTTP bind address. Loopback only by default. |
| `MCP_PORT` | `6543` | HTTP port. |
| `MCP_PATH` | `/mcp` | HTTP URL path. |
| `MCP_INDEX_INTERVAL_SECONDS` | `0` (off) | HTTP transports only. Index new mail inside the server every this many seconds (`300` works well), so the server is the only process that opens the index. While it runs, CLI commands that open the index refuse to start; the value must be 0 or 30 to 86400. See [Index inside the MCP server](scheduling.md#index-inside-the-mcp-server). |
| `MCP_IDLE_UNLOAD_SECONDS` | `600` | How long the server keeps the vector index and models in memory after the last tool call. The index is held fully in RAM (several GB for a large mailbox), so an idle server releases it and reloads it on the next call in a few seconds. `0` keeps everything loaded. Either way, the server picks up new index runs automatically. |

## Logging

Normal CLI output stays in the terminal. Internal warnings, diagnostics and tracebacks go to `LOG_PATH`.

| Variable | Default | Description |
|---|---|---|
| `LOG_PATH` | `./data/logs/mail-semantic-search.error.log` | Log file. If it isn't writable, the app falls back to `./data/logs`, then the system temp directory, and warns once. |
| `LOG_LEVEL` | `INFO` | App log level. |
| `LOG_THIRD_PARTY_LEVEL` | `WARNING` | Log level for third-party libraries. |
| `LOG_MAX_BYTES` | `10485760` | Rotate the log at this size. |
| `LOG_BACKUP_COUNT` | `5` | Rotated logs to keep. |

## Switching models

1. Change `EMBEDDING_MODEL` in `.env`.
2. Rebuild the index: `mail-semantic-search index --no-skip` (or `docker compose run --rm mail-semantic-search index --no-skip`).

The new model downloads into `MODEL_CACHE_DIR` on the first run that needs it, then loads from the cache from then on.
