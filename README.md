# MailMate AI Search Tool

AI-powered semantic search for MailMate emails using local embeddings and vector search.

## Features

- **Local AI Embeddings**: Uses sentence-transformers models (default: BGE-base-en-v1.5) running entirely on your machine
- **Semantic Search**: Find emails by meaning, not just keywords
- **MCP Server**: Expose search/query/status as local MCP tools for editors and agents
- **Dockerized**: Easy setup with Docker and Docker Compose
- **Incremental Indexing**: Only indexes new or changed emails
- **Privacy-First**: All processing happens locally, no external APIs

## Requirements

- Docker and Docker Compose
- Mac Mini M4 with 24GB RAM (or similar hardware)
- MailMate email client with emails stored as .eml files

## Quick Start

1. **Clone or navigate to the project directory**

2. **Create `.env` file:**
   ```bash
   # Create .env file with the following content:
   # Note: MAILMATE_EMAIL_DIR must be an absolute path (not ~)
   cat > .env << 'EOF'
   EMBEDDING_MODEL=BGE-base-en-v1.5
   MAILMATE_EMAIL_DIR=/Users/yourusername/Library/Application Support/MailMate/Messages
   CHROMADB_PATH=./data/chromadb
   MODEL_CACHE_DIR=./data/models
   BATCH_SIZE=32
   SEARCH_RESULTS=10
   EOF
   ```
   **Important**: Replace `/Users/yourusername` with your actual home directory path. The `MAILMATE_EMAIL_DIR` must be an absolute path for Docker volume mounting.

3. **Edit `.env` file:**
   - Set `MAILMATE_EMAIL_DIR` to your MailMate messages directory
   - Adjust other settings as needed

4. **Build the container image:**
   ```bash
   docker compose build
   ```

5. **Index your emails:**
   ```bash
   docker compose run --rm mailmate-search index
   ```
   This may take several hours for large email collections (35GB = ~700k emails). Indexing shows percent complete by default.

6. **Search your emails:**
   ```bash
   docker compose run --rm mailmate-search search "your query here"
   ```

Because this project is a CLI container (not a long-running daemon), `docker compose up -d` will start, print help, and exit. Use `docker compose run --rm ...` for commands.

## Configuration

All configuration is done via the `.env` file:

- `EMBEDDING_MODEL`: Embedding model to use (default: `BGE-base-en-v1.5`)
  - Options: `BGE-base-en-v1.5` (best quality), `BGE-small-en-v1.5`, `nomic-embed-text-v1`, `all-MiniLM-L6-v2`
- `MAILMATE_EMAIL_DIR`: Path to MailMate messages directory
- `CHROMADB_PATH`: Where to store ChromaDB data
- `MODEL_CACHE_DIR`: Where to cache downloaded models
- `BATCH_SIZE`: Number of emails to process at once (default: 32)
- `SEARCH_RESULTS`: Number of results to return (default: 10)
- `QUERY_PARSER_ENABLED`: Enable local LLM query-to-filter parsing (default: `false`)
- `QUERY_PARSER_ENDPOINT`: Local parser endpoint (default: `http://localhost:11434/api/generate`)
- `QUERY_PARSER_MODEL`: Local parser model name (default: `llama3.1:8b`)
- `QUERY_PARSER_TIMEOUT_SECONDS`: Timeout for parser call in seconds (default: `8`)
- `RERANK_ENABLED`: Enable local cross-encoder reranking (default: `false`)
- `RERANKER_MODEL`: Cross-encoder model (default: `cross-encoder/ms-marco-MiniLM-L-6-v2`)
- `RERANK_MAX_CANDIDATES`: Candidate pool size before rerank (default: `50`)
- `RERANK_MAX_TEXT_CHARS`: Max candidate text length passed to reranker (default: `1200`)
- `INCREMENTAL_OVERLAP_SECONDS`: Re-scan window subtracted from the incremental watermark to catch clock skew and odd file writes (default: `86400`)
- `LOG_PATH`: Runtime log file for internal warnings/errors/diagnostics (default: `./data/logs/mailmate-search.error.log`)
- `LOG_LEVEL`: App log verbosity written to `LOG_PATH` (default: `INFO`)
- `LOG_THIRD_PARTY_LEVEL`: Third-party library log verbosity written to `LOG_PATH` (default: `WARNING`)
- `LOG_MAX_BYTES`: Log rotation size threshold in bytes (default: `10485760`)
- `LOG_BACKUP_COUNT`: Number of rotated log files to retain (default: `5`)

Normal CLI output stays in the terminal. Internal warnings, diagnostics, and traceback dumps are written to `LOG_PATH`.

## Commands

- `index`: Index emails from MailMate directory
  - `--limit N`: Limit indexing to N emails (for testing)
  - `--no-skip`: Re-index all emails even if already indexed (full rebuild behavior)
  - `--incremental`: Only scan files newer than the saved incremental watermark minus an overlap window

Incremental behavior (`index --incremental`):
- Uses the last successful incremental scan time as the primary watermark.
- Falls back to the maximum indexed file mtime for older indexes that predate the watermark.
- Scans files whose filesystem mtime is newer than `watermark - INCREMENTAL_OVERLAP_SECONDS` (default: 24 hours).
- Advances the watermark only after a fully successful incremental run.
- Still writes updates by file path and vector ID, so reruns stay idempotent for changed files in the candidate set.

- `search "query"`: Search for emails matching the query
  - `--auto-filters/--no-auto-filters`: Override local parser toggle per query
  - `--rerank/--no-rerank`: Override local reranker toggle per query

- `query`: Query emails using metadata filters only

- `inspect --file-path "/full/path/to/email.eml"`: Show the indexed SQLite metadata and exact Chroma document for one email

- `status`: Show indexing status and statistics

## MCP Server

This project can also run as a local **stdio MCP server**. The MCP process is started by the client when needed; it does not require a separate always-on database service.

Storage/runtime model:
- SQLite is opened directly from disk by the Python process
- Chroma uses the local persistent store on disk
- The reranker loads in-process
- The query parser only needs a separate local service if you keep `QUERY_PARSER_ENDPOINT` pointed at something like Ollama

Install dependencies in your local Python environment, then run:

```bash
mailmate-search-mcp
```

Recommended initial MCP tools:
- `search_emails`: semantic search with optional auto-filters and reranking
- `query_emails`: metadata-only lookup
- `get_status`: index and configuration summary

### Claude Desktop (macOS)

Claude Desktop reads MCP server config from:

```bash
~/Library/Application Support/Claude/claude_desktop_config.json
```

Add a `mailmate-search` entry under `mcpServers`.

**Option A — local venv (simplest when the MCP client can read your `DATABASE_PATH` / `CHROMADB_PATH`):**

```json
{
  "mcpServers": {
    "mailmate-search": {
      "command": "/bin/zsh",
      "args": [
        "-lc",
        "cd /Users/yourusername/Development/mailmate-search && .venv/bin/mailmate-search-mcp"
      ]
    }
  }
}
```

**Option B — MCP via Docker (same process environment as `docker compose run … index/search`, one dataset on the bind-mounted volume):**

Indexing already uses Compose mounts and `/app/data/...` inside the container. Running MCP the same way avoids the host MCP client having to open SQLite/Chroma under `/Volumes/...` at all. Use **`-i`** (keep stdin open for MCP) and **`-T`** (no pseudo-TTY):

```json
{
  "mcpServers": {
    "mailmate-search": {
      "command": "/bin/zsh",
      "args": [
        "-lc",
        "cd /Users/yourusername/Development/mailmate-search && docker compose run --rm -i -T --entrypoint python mailmate-search -m mailmate_search.mcp_server"
      ]
    }
  }
}
```

Docker Desktop must be running; the first tool call may wait for a short container cold start.

Notes:
- Replace `/Users/yourusername` with your real home directory.
- **One project, one data directory:** Compose still resolves `${MAILMATE_EMAIL_DIR}` and `env_file: .env` from the repo when you `cd` there first.
- **Why MCP is not “in Docker” by default:** the MCP client spawns whatever command you configure. A local venv is the usual default; Option B is the supported way to align MCP with the same container mounts as indexing when the client blocks direct access to external volumes.
- **If Option A shows zero indexed emails while Docker search works:** your host `.env` paths did not match the compose bind mount; align them or use Option B.
- If you prefer, you can launch the module directly instead of the console script: `cd /Users/yourusername/Development/mailmate-search && .venv/bin/python -m mailmate_search.mcp_server`
- After editing the config, fully quit and reopen Claude Desktop.

Phase 3 answer synthesis should stay separate from retrieval, either as a future MCP tool like `answer_question` or a separate CLI command.

## Storage Requirements

For 35GB of email data (~700,000 emails):
- Embeddings: ~2.1GB (with BGE-base-en-v1.5)
- Model files: ~420MB
- Metadata: ~100-200MB
- **Total**: ~2.5GB additional storage

## Model Switching

To switch embedding models:
1. Update `EMBEDDING_MODEL` in `.env`
2. Re-run indexing: `docker compose run --rm mailmate-search index --no-skip`

The system will automatically download and use the new model.

## Troubleshooting

**MailMate directory not found:**
- Check that `MAILMATE_EMAIL_DIR` in `.env` points to the correct path
- Default location: `~/Library/Application Support/MailMate/Messages`

**Out of memory:**
- Reduce `BATCH_SIZE` in `.env`
- Use a smaller model like `BGE-small-en-v1.5`

**Slow indexing:**
- This is normal for large collections (several hours for 35GB)
- The process can be interrupted and resumed
- Already indexed emails are skipped on subsequent runs
- Use `--no-skip` when you want a full rebuild across all files

**Warnings/errors while indexing:**
- Progress bars and high-level status updates stay in the terminal
- Internal warnings, diagnostics, and traceback dumps go to `LOG_PATH`

**MCP / Claude and `LOG_PATH` on an external volume (`/Volumes/...`):**
- A log file can be recently updated by Docker or a terminal session while **Claude’s MCP subprocess** still gets `PermissionError: Operation not permitted` opening the same path. That is normal on macOS: different apps (and sandboxed MCP hosts) do not share the same TCC / filesystem access as Docker Desktop.
- Point **`LOG_PATH` at something inside the repo** (default `./data/logs/...`) or under your home directory so MCP can always open it. If the configured `LOG_PATH` is not writable, the app falls back automatically (repo `./data/logs`, then the system temp directory) and logs a warning once.

**MCP / Claude and `DATABASE_PATH` / `CHROMADB_PATH` on `/Volumes/...`:**
- If MCP logs `unable to open database file` (SQLite) or Chroma fails while `docker compose run` still works, the **MCP parent app** may be blocked from that path even though Docker is not. You still have **one dataset**; run MCP **inside** Compose (README **Option B — MCP via Docker**) or grant **Full Disk Access** to the Claude app, or point host-only paths under `~/...` if you use a local venv MCP.

## License

MIT

