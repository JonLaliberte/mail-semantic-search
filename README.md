# mail-semantic-search

Search your local email archive by **meaning** and by **metadata** — locally. Combines vector search over message bodies and attachment text with structured filters (from / to / subject / date / has-attachments / attachment type / attachment name). Use it from the CLI or wire it into Claude Desktop and other MCP-aware agents so they can query your inbox like a database. No email data ever leaves your machine.

## Features

- **Local AI Embeddings**: Uses sentence-transformers models (default: BGE-base-en-v1.5) running entirely on your machine
- **Semantic Search**: Find emails by meaning, not just keywords
- **MCP Server**: Expose search/query/status as local MCP tools for editors and agents
- **Dockerized**: Easy setup with Docker and Docker Compose
- **Incremental Indexing**: Only indexes new or changed emails
- **Privacy-First**: All processing happens locally, no external APIs

## Requirements

- macOS or Linux (developed and tested on macOS)
- **Either** Docker + Docker Compose **or** Python 3.10+ for the native venv
- An email client that stores messages as `.eml` files (e.g. MailMate)
- 16 GB+ RAM recommended (8 GB may work for small archives); Apple Silicon or modern x86_64
- Tested on: Mac Mini M4, 24 GB RAM
- Disk for the index: roughly **~2.5 GB per 35 GB of email** (~700k messages) — split across embeddings (~2.1 GB), model files (~420 MB), and metadata (~100–200 MB)

## Quick Start

1. **Clone or navigate to the project directory**

2. **Create `.env` file:**
   ```bash
   # Create .env file with the following content:
   # Note: EMAIL_DIR must be an absolute path (not ~)
   cat > .env << 'EOF'
   EMBEDDING_MODEL=BGE-base-en-v1.5
   EMAIL_DIR=/Users/yourusername/Library/Application Support/MailMate/Messages
   DATA_VOLUME_PATH=./data
   BATCH_SIZE=32
   SEARCH_RESULTS=10
   EOF
   ```
   **Important**: Replace `/Users/yourusername` with your actual home directory path. The `EMAIL_DIR` must be an absolute path for Docker volume mounting.
   Alternatively, copy `.env.example` to `.env` and edit it.

3. **Edit `.env` file:**
   - Set `EMAIL_DIR` to your email messages directory
   - Adjust other settings as needed

4. **Pull the published image:**
   ```bash
   docker compose pull
   ```

5. **Index your emails:**
   ```bash
   docker compose run --rm mail-semantic-search index
   ```
   This may take several hours for large email collections (35GB = ~700k emails). Indexing shows percent complete by default.

6. **Search your emails:**
   ```bash
   docker compose run --rm mail-semantic-search search "your query here"
   ```

Because this project is a CLI container (not a long-running daemon), `docker compose up -d` will start, print help, and exit. Use `docker compose run --rm ...` for commands.

## Configuration

All configuration is done via the `.env` file:

- `EMBEDDING_MODEL`: Embedding model to use (default: `BGE-base-en-v1.5`)
  - Options: `BGE-base-en-v1.5` (best quality), `BGE-small-en-v1.5`, `nomic-embed-text-v1`, `all-MiniLM-L6-v2`
- `EMAIL_DIR`: Path to your email messages directory (must contain .eml files)
- `DATA_VOLUME_PATH`: Host path Docker bind-mounts as `/app/data` (default: `./data`). Set to an absolute path when storing index data on an external drive, e.g. `/Volumes/My Drive/mail-semantic-search/data`
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
- `BODY_PREVIEW_LIMIT`: Max chars stored in `body_preview` (default: `5000`). The embedding text is independently capped by the model's context window (~2000 chars for BGE-base), so bumping this affects snippet display only.
- `STAGING_DIR`: Where `stage_email_attachments` (MCP) / `stage` (CLI) copies an email's attachments + `.eml` for sandbox-accessible reads (default: `~/Documents/mailmate-staged`)
- `MCP_TRANSPORT`: `stdio` (default — spawned by client) or `http` (standalone HTTP server, see "MCP over HTTP" below)
- `MCP_HOST`: HTTP bind address when `MCP_TRANSPORT=http` (default: `127.0.0.1`, loopback only)
- `MCP_PORT`: HTTP port (default: `6543`)
- `MCP_PATH`: HTTP URL path (default: `/mcp`)
- `LOG_PATH`: Runtime log file for internal warnings/errors/diagnostics (default: `./data/logs/mail-semantic-search.error.log`)
- `LOG_LEVEL`: App log verbosity written to `LOG_PATH` (default: `INFO`)
- `LOG_THIRD_PARTY_LEVEL`: Third-party library log verbosity written to `LOG_PATH` (default: `WARNING`)
- `LOG_MAX_BYTES`: Log rotation size threshold in bytes (default: `10485760`)
- `LOG_BACKUP_COUNT`: Number of rotated log files to retain (default: `5`)

Normal CLI output stays in the terminal. Internal warnings, diagnostics, and traceback dumps are written to `LOG_PATH`.

## Releases

Versioning is automatic: every merge to `main` with a [Conventional Commit](https://www.conventionalcommits.org/) title is analyzed by [python-semantic-release](https://python-semantic-release.readthedocs.io/), which computes the next semantic version, tags it `vX.Y.Z`, and publishes a GitHub Release plus the Docker image below — no manual version bumps. (Maintainer/agent details live in `AGENTS.md`.)

Tagged releases are published as multi-arch (amd64 + arm64) Docker images to GitHub Container Registry:

```
ghcr.io/jonlaliberte/mail-semantic-search
```

Available tags:
- `:latest` — most recent stable release
- `:0.5`, `:0.5.0` — minor and exact-version aliases (replace with whatever the current release is)

For production, pin to an exact version in `docker-compose.yml`:

```yaml
image: ghcr.io/jonlaliberte/mail-semantic-search:0.5.0
```

Release notes (including the changelog generated from commits and PRs) live on the [GitHub Releases page](https://github.com/JonLaliberte/mail-semantic-search/releases).

## Commands

- `index`: Index emails from the configured email directory
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

- `index-file <path>`: Index a single `.eml` file end-to-end (parse, dedup-check, embed, write). Designed for MailMate rules that fire on new mail — `mail-semantic-search index-file "$MM_PATH"` indexes the message immediately instead of waiting for the next scheduled incremental scan.
  - `--force`: Re-embed and re-upsert even if the stored mtime matches

- `inspect --file-path "/full/path/to/email.eml"`: Show the indexed SQLite metadata and exact Chroma document for one email

- `status`: Show indexing status and statistics

- `dedup`: Remove duplicate index entries that share the same `Message-ID`, keeping the most recently indexed copy. Run `--dry-run` first to preview.

- `prune`: Remove index entries whose backing `.eml` file no longer exists on disk. The SQLite table never drops vanished files on its own, so over time it accumulates orphaned rows for emails deleted or moved in MailMate, drifting above the ChromaDB count. `prune` scans the mail directory once and deletes the orphaned rows (and their ChromaDB vectors), reconciling the two counts. Aborts if the mail directory is missing or the scan finds zero files (so an unmounted drive can't wipe the index). Run `--dry-run` first to preview; `--batch-size N` controls the delete-commit batch (default 1000). Idempotent.

- `reextract`: Re-parse and re-embed already-indexed emails using the current extractor. Use after bumping `CURRENT_EXTRACTION_VERSION` in `mailmate_reader.py` (see AGENTS.md). Two modes:
  - **Single-email** (visual QA, prints before/after `body_preview` diff):
    `reextract --file-path "/path/to.eml"` or `reextract --message-id "<...@...>"`
  - **Bulk** (walks every row where `extraction_version < CURRENT_EXTRACTION_VERSION`):
    `reextract` — optional `--limit N`, `--batch-size 64`, `--dry-run`
  - Resumable: completed rows are bumped to the current version, so re-running picks up where it left off.
  - Holds a **backfill lock** in `app_state` for the whole run; concurrent `index --incremental` calls print a benign "Backfill in progress; skipping" line and exit 0 (no `error`/`warning` substrings, so Keyboard Maestro / launchd job watchers don't pop alerts).

- `stage`: Copy an indexed email's attachments + `.eml` to a sandbox-accessible path under `STAGING_DIR` (default `~/Documents/mailmate-staged/<short-hash>/`). Use when an MCP client's filesystem sandbox can't read the source `.eml` (e.g. an external volume that lacks Full Disk Access for the client process).
  - `stage --file-path "/path/to.eml"` or `stage --message-id "<...@...>"`
  - `--no-eml` to skip copying the `.eml` itself (attachments only)
  - Idempotent: same email always stages to the same directory.

- `clear-staged`: Remove staged email directories. `--short-hash X` for a single email; omit for all.

## MCP Server

This project can also run as a local **stdio MCP server**. The MCP process is started by the client when needed; it does not require a separate always-on database service.

Storage/runtime model:
- SQLite is opened directly from disk by the Python process
- Chroma uses the local persistent store on disk
- The reranker loads in-process
- The query parser only needs a separate local service if you keep `QUERY_PARSER_ENDPOINT` pointed at something like Ollama

Install dependencies in your local Python environment, then run:

```bash
mail-semantic-search-mcp
```

Available MCP tools:
- `search_emails`: semantic search with optional auto-filters and reranking
- `query_emails`: metadata-only lookup
- `list_inbox_emails`: newest-first inbox listing with optional account / date-range pagination (caller pages by feeding the oldest result's `date` back as `date_before`)
- `get_status`: index and configuration summary
- `stage_email_attachments`: copy an email's attachments + `.eml` to `STAGING_DIR` so a sandboxed client can `Read` the bytes (see `stage` CLI command above for the same operation)
- `clear_staged_emails`: remove staged dirs (single via `short_hash` or all)
- macOS only (when running on Darwin): `open_email`, `mark_email_read`, `archive_email`, `mark_read_and_archive`

### Claude Desktop (macOS)

Claude Desktop reads MCP server config from:

```bash
~/Library/Application Support/Claude/claude_desktop_config.json
```

Add a `mail-semantic-search` entry under `mcpServers`.

**Option A — local venv (simplest when the MCP client can read your `DATABASE_PATH` / `CHROMADB_PATH`):**

```json
{
  "mcpServers": {
    "mail-semantic-search": {
      "command": "/bin/zsh",
      "args": [
        "-lc",
        "cd /Users/yourusername/Development/mail-semantic-search && .venv/bin/mail-semantic-search-mcp"
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
    "mail-semantic-search": {
      "command": "/bin/zsh",
      "args": [
        "-lc",
        "cd /Users/yourusername/Development/mail-semantic-search && docker compose run --rm -i -T --entrypoint python mail-semantic-search -m mail_semantic_search.mcp_server"
      ]
    }
  }
}
```

Docker Desktop must be running; the first tool call may wait for a short container cold start.

Notes:
- Replace `/Users/yourusername` with your real home directory.
- **One project, one data directory:** Compose still resolves `${EMAIL_DIR}` and `env_file: .env` from the repo when you `cd` there first.
- **Why MCP is not “in Docker” by default:** the MCP client spawns whatever command you configure. A local venv is the usual default; Option B is the supported way to align MCP with the same container mounts as indexing when the client blocks direct access to external volumes.
- **If Option A shows zero indexed emails while Docker search works:** your host `.env` paths did not match the compose bind mount; align them or use Option B.
- If you prefer, you can launch the module directly instead of the console script: `cd /Users/yourusername/Development/mail-semantic-search && .venv/bin/python -m mail_semantic_search.mcp_server`
- After editing the config, fully quit and reopen Claude Desktop.

### MCP over HTTP (Full Disk Access workaround for macOS)

By default the MCP server runs over **stdio** — your MCP client spawns the process. macOS Full Disk Access does **not** inherit across `spawn()`, so if your maildir lives on an external volume and the MCP client (Claude Desktop, etc.) is not itself granted FDA for that volume, every read of an `.eml` blows up with `Operation not permitted`. Granting FDA to a specific spawned subprocess is unreliable.

The escape hatch: run the MCP server standalone over HTTP from a process that **does** have FDA (a terminal, Keyboard Maestro, launchd) and have the MCP client connect via a stdio→HTTP bridge.

**1. Start the HTTP server** (from a terminal that has FDA):

```bash
MCP_TRANSPORT=http mail-semantic-search-mcp
```

Output: `Mail Semantic Search MCP listening on http://127.0.0.1:6543/mcp`. Bound to loopback only.

For auto-start at login, the simplest path is Keyboard Maestro with an "At Login" trigger running an **Execute Shell Script** action:

```sh
#!/bin/bash
PORT="${MCP_PORT:-6543}"
LOG="$HOME/Library/Logs/mailmate-search-mcp.log"
BIN="/Users/yourusername/Development/mail-semantic-search/.venv/bin/mail-semantic-search-mcp"
mkdir -p "$(dirname "$LOG")"
# Idempotent: don't spawn a duplicate.
if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "$(date '+%F %T') MCP already listening on $PORT, skipping" >> "$LOG"
  exit 0
fi
echo "$(date '+%F %T') starting MCP on port $PORT" >> "$LOG"
MCP_TRANSPORT=http MCP_PORT="$PORT" \
  nohup "$BIN" >> "$LOG" 2>&1 &
disown
```

Optionally add a second **Periodic every 5 minutes** trigger running the same script — the `lsof` check makes re-firing free, and the server auto-recovers if it ever dies.

**2. Configure Claude Desktop** to use a stdio→HTTP bridge. Claude Desktop does **not** natively speak HTTP MCP — silently ignoring a bare `"url"` field. Use `mcp-remote` (npm-installed, runs via `npx`):

```json
{
  "mcpServers": {
    "mail-semantic-search": {
      "command": "/Users/yourusername/.nvm/versions/node/v22.20.0/bin/npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:6543/mcp"]
    }
  }
}
```

The absolute path to `npx` matters: Claude Desktop's spawn env does **not** include your shell `PATH`, so plain `"npx"` won't resolve when node is installed via nvm. Adjust the node version path for your machine.

The bridge process has zero filesystem needs — it just relays JSON-RPC — so it does not need FDA. The HTTP server keeps its FDA via KM's grant.

Python equivalent if you'd rather not depend on npm: `pip install mcp-proxy` and use `mcp-proxy` instead of `npx mcp-remote`.

## Keeping Your Index Fresh

The MCP server and CLI search only what has been indexed. Running `index --incremental` regularly keeps results current as new mail arrives. Once a day is the minimum; every few hours is better.

There are two ways to run it. Both produce identical file_path values in the index (the container mounts the maildir at the same path it has on the host), so you can mix and match freely.

### Native venv (recommended for scheduled runs)

Faster: ~10–30 seconds per incremental run instead of ~2 minutes, because `find` walks the maildir directly on the host filesystem instead of through Docker's macOS filesystem shim.

```bash
cd /Users/yourusername/Development/mail-semantic-search && .venv/bin/mail-semantic-search index --incremental
```

Requires that you've created a local venv (`python3 -m venv .venv && .venv/bin/pip install -e .`) — the same one used by the MCP server in Option A below.

### Docker

Slower but no Python toolchain needed locally. Use this if you only have Docker.

```bash
cd /Users/yourusername/Development/mail-semantic-search && /opt/homebrew/bin/docker compose run --rm mail-semantic-search index --incremental
```

> **Note:** Use the full path to `docker` (`/opt/homebrew/bin/docker` on Apple Silicon Homebrew installs). Cron and launchd do not inherit your shell `PATH`.

### Option A — cron (quick)

Edit your crontab with `crontab -e` and add one line. This example runs every hour:

```
0 * * * * cd /Users/yourusername/Development/mail-semantic-search && /opt/homebrew/bin/docker compose run --rm mail-semantic-search index --incremental >> /tmp/mail-semantic-search-index.log 2>&1
```

To run once daily at 9am instead, change the schedule to `0 9 * * *`.

### Option B — launchd (robust, macOS-native)

Create `~/Library/LaunchAgents/com.mail-semantic-search.index.plist` with the following content (replace `yourusername`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mail-semantic-search.index</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-lc</string>
        <string>cd /Users/yourusername/Development/mail-semantic-search &amp;&amp; /opt/homebrew/bin/docker compose run --rm mail-semantic-search index --incremental</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/mail-semantic-search-index.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mail-semantic-search-index.error.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.mail-semantic-search.index.plist
```

To unload: `launchctl unload ~/Library/LaunchAgents/com.mail-semantic-search.index.plist`

### Option C — Keyboard Maestro

For users who already have [Keyboard Maestro](https://www.keyboardmaestro.com/):

1. Create a new macro with a **Time of Day** trigger (or **Periodic** trigger for sub-hourly).
2. Add an **Execute Shell Script** action. Set shell to `/bin/zsh`.
3. Paste the full command:
   ```
   cd /Users/yourusername/Development/mail-semantic-search && /opt/homebrew/bin/docker compose run --rm mail-semantic-search index --incremental
   ```
4. Optionally pipe output to a log file by appending `>> /tmp/mail-semantic-search-index.log 2>&1`.

No PATH issues since the full docker path is explicit.

## Model Switching

To switch embedding models:
1. Update `EMBEDDING_MODEL` in `.env`
2. Re-run indexing: `docker compose run --rm mail-semantic-search index --no-skip`

The system will automatically download and use the new model.

## Troubleshooting

**Email directory not found:**
- Check that `EMAIL_DIR` in `.env` points to the correct path
- MailMate default: `~/Library/Application Support/MailMate/Messages`

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

