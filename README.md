# mail-semantic-search

Local semantic search for email files using local embeddings and vector search.

## Features

- **Local AI Embeddings**: Uses sentence-transformers models (default: BGE-base-en-v1.5) running entirely on your machine
- **Semantic Search**: Find emails by meaning, not just keywords
- **MCP Server**: Expose search/query/status as local MCP tools for editors and agents
- **Dockerized**: Easy setup with Docker and Docker Compose
- **Incremental Indexing**: Only indexes new or changed emails
- **Privacy-First**: All processing happens locally, no external APIs

## Requirements

- Docker and Docker Compose
- An email client that stores messages as .eml files (e.g. MailMate)
- **Tested on:** Mac Mini M4, 24GB RAM
- **Minimum recommended:** 16GB RAM (8GB may work for small collections); Apple Silicon or modern x86_64

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

4. **Pull the published image** (or build from source):
   ```bash
   docker compose pull
   ```
   To build locally instead: `docker compose build`.

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
- `LOG_PATH`: Runtime log file for internal warnings/errors/diagnostics (default: `./data/logs/mail-semantic-search.error.log`)
- `LOG_LEVEL`: App log verbosity written to `LOG_PATH` (default: `INFO`)
- `LOG_THIRD_PARTY_LEVEL`: Third-party library log verbosity written to `LOG_PATH` (default: `WARNING`)
- `LOG_MAX_BYTES`: Log rotation size threshold in bytes (default: `10485760`)
- `LOG_BACKUP_COUNT`: Number of rotated log files to retain (default: `5`)

Normal CLI output stays in the terminal. Internal warnings, diagnostics, and traceback dumps are written to `LOG_PATH`.

## Releases

Tagged releases are published as multi-arch (amd64 + arm64) Docker images to GitHub Container Registry:

```
ghcr.io/jonlaliberte/mail-semantic-search
```

Available tags:
- `:latest` — most recent stable release
- `:0.2`, `:0.2.0` — minor and exact-version aliases

For production, pin to an exact version in `docker-compose.yml`:

```yaml
image: ghcr.io/jonlaliberte/mail-semantic-search:0.2.0
```

Release notes (including the changelog generated from commits and PRs) live on the [GitHub Releases page](https://github.com/JonLaliberte/mail-semantic-search/releases).

### Releasing a new version (maintainers)

```bash
# 1. Bump version in pyproject.toml, commit it
# 2. Tag and push
git tag -a v0.3.0 -m "Release 0.3.0"
git push origin v0.3.0
# 3. CI builds the multi-arch image, pushes to GHCR, and creates a GitHub Release
```

The first time a release is published the GHCR package will be private — flip it to Public once in the GitHub UI (Packages → Package settings → Change visibility). Subsequent publishes are automatic.

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
mail-semantic-search-mcp
```

Recommended initial MCP tools:
- `search_emails`: semantic search with optional auto-filters and reranking
- `query_emails`: metadata-only lookup
- `list_inbox_emails`: newest-first inbox listing with optional account / date-range pagination (caller pages by feeding the oldest result's `date` back as `date_before`)
- `open_email_url`: open a `message://` URL with the OS-default mail client
- `get_status`: index and configuration summary

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

## Keeping Your Index Fresh

The MCP server and CLI search only what has been indexed. Running `index --incremental` regularly keeps results current as new mail arrives. Once a day is the minimum; every few hours is better.

**Command to schedule** (replace `yourusername` with your home directory):

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

## Storage Requirements

For 35GB of email data (~700,000 emails):
- Embeddings: ~2.1GB (with BGE-base-en-v1.5)
- Model files: ~420MB
- Metadata: ~100-200MB
- **Total**: ~2.5GB additional storage

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

