# MCP server

`mail-semantic-search-mcp` exposes your index to AI clients over the [Model Context Protocol](https://modelcontextprotocol.io). By default it's a **stdio** server: the client starts it when needed, and there's no separate service to keep running.

Everything runs in that one process. SQLite and ChromaDB are opened from disk, and the embedding model and optional reranker load in-process. The only optional external dependency is the query parser, if you point `QUERY_PARSER_ENDPOINT` at a local LLM such as Ollama.

## Tools

| Tool | What it does |
|---|---|
| `search_emails` | Meaning-based search with optional metadata filters, local query parsing and reranking. Use it when the request is about content or concepts. |
| `query_emails` | Fast metadata-only lookup by sender, recipient, subject, date and attachments. Capped at `MAX_FILTERED_SEARCH_LIMIT` (1000); the response reports the applied `limit` and whether results were `truncated`. |
| `list_inbox_emails` | Newest-first list of messages currently in MailMate IMAP INBOX folders, with optional account filter and date-bound pagination. |
| `get_status` | Index and configuration summary. |
| `get_email_attachments` | Read attachment bytes from an indexed email and return them as base64 for the client to decode. |
| `stage_email_attachments` | Copy an email's attachments (and optionally its `.eml`) to `STAGING_DIR` so a sandboxed client can read them. |
| `clear_staged_emails` | Delete staged copies: one by `short_hash`, or all. Source emails and the index are never touched. |
| `open_email` | macOS only. Open a message in MailMate (visible, read-only). |
| `mark_email_read`, `archive_email`, `mark_read_and_archive` | macOS only. Change message state in MailMate. |

### Fetch attachment bytes

Call `get_email_attachments` with a `message_id` (with or without angle brackets) or exact indexed `file_path` from a search result. If you send both, they must identify the same indexed row. The resolved source must be an `.eml` under `EMAIL_DIR`. The server reads the source and returns a response without staging copies.

Use `attachment_ids` from `query_emails` or exact decoded `filenames` to select attachments. Both filters apply when provided. An empty list selects no attachments. Pass the exact `file_path` from the same result when selecting by attachment IDs. If several indexed copies share a Message-ID, an ID-only message lookup can select a different copy. Re-indexing recreates the attachment rows and can change their IDs, so query again if the IDs have gone stale. `unmatched_attachment_ids` lists requested IDs that could not be mapped to MIME parts in the selected copy, including stale IDs and metadata that no longer matches the source. To recover attachments when IDs stay unmatched, retry with the exact `file_path` and decoded filenames.

When an ID cannot be assigned safely, `unmatched_attachment_id_reasons` can report `ambiguous` for competing ID or MIME part matches, or `size_unknown` when the part's indexed size cannot be determined. The tool omits those attachments from ID-filtered results.

With neither filter, the tool walks the MIME tree in message order and includes attachment and inline leaf parts. It omits empty parts and multipart containers, including the attached message itself when `message/rfc822` is parsed as a nested message.

Read the result from the MCP response's `structuredContent`. Each entry includes `id` (when matched to the index), `filename`, `safe_name`, MIME `content_type`, decoded byte `size`, and, on success, `sha256` (hex) and `data_base64`. Use only `safe_name` as the basename when saving to a client-controlled directory. The original `filename` is for display and can contain path separators. A skipped entry has null data and hash and reports `too_large`, `over_total` or `unreadable` in `skipped`. On success `skipped` is null. An unreadable file may have a null size. Set `include_eml=true` to also return the raw message in the separate `eml` field, using the same limits and the budget left after attachments. With `include_eml=false`, `eml` is null.

Attachment bytes follow the same MIME parsing as local staging, including newline handling. The raw message preserves the original source bytes. Attachment and raw message `safe_name` values are unique within a response, including on case-insensitive filesystems. The tool renames Windows device basenames and strips trailing dots and spaces. The raw message normally uses `message.eml` and gets a numbered suffix when that name is taken.

The default caps are 10 MiB per file and 25 MiB combined, measured before base64 encoding. Entries that exceed either cap do not consume the budget, so later files can still fit. Clients can lower the caps with `max_bytes_per_file` and `max_total_bytes`. Operators can change the server caps and disable the two MCP staging tools with `DISABLE_STAGING_TOOLS=true`. [Attachment settings](configuration.md#attachment-retrieval-over-mcp)

The top-level `status` is `ok` when the source could be read, even if entries were skipped. Other statuses are `not_indexed`, `source_missing`, `source_outside_email_dir` (including a non-`.eml` path), `source_unreadable`, and `source_too_large`. Sources above `MAX_EMAIL_FILE_SIZE` are rejected before MIME parsing. When `include_eml=true`, an oversized source also has `eml.skipped` set to `too_large`. When configuring a client or proxy with a tool allowlist, give `get_email_attachments` only to clients that should receive the source bytes.

### Stage attachments on the client

When the search index runs on another machine, launch `mail-semantic-search-stager` as a local stdio MCP server. It authenticates to the remote HTTPS MCP endpoint with a bearer token, writes fetched files under the local output directory, and returns attachment names, paths, sizes and skipped reasons.

```json
{
  "mcpServers": {
    "mail-stage": {
      "command": "/path/to/.venv/bin/mail-semantic-search-stager",
      "args": [
        "--endpoint", "https://mail-search.example/mcp",
        "--token-file", "/path/to/private/mcp-token",
        "--ca-file", "/path/to/server-ca.pem",
        "--output-dir", "/path/to/mail-attachments"
      ]
    }
  }
}
```

Use a private token file and an output directory visible to your client's file tools. The launch command works with Claude Desktop, Claude Code and other stdio clients. For a standalone setup, install FastMCP 3.3.1 or later in a Python environment and run `attachment_stager.py` with that Python and the same arguments.

The stager exposes `stage_email_attachments` and `clear_staged_emails`. Stage by Message-ID or exact indexed path, optionally with attachment IDs or filenames. Use `include_eml=false` for attachments only. All files remain subject to the remote caps. Each email stages into a stable twelve-character directory, which you can pass as `short_hash` to remove that email's staged copies. Omitting the hash clears staged email directories under the configured output root.

### Filter semantics (`search_emails` and `query_emails`)

- `from_addr`, `to_addr`, `subject_like` and `attachment_name` are partial matches. `to_addr` covers To, CC and BCC.
- `subject` and `attachment_type` are exact matches. Attachment extensions may include a leading dot.
- Date bounds are inclusive and accept ISO-8601 dates or timestamps.
- Filters in one call are combined with AND. For "to or from" searches, make two calls and deduplicate by `message_id`.

### MailMate deep links

Every result from `search_emails`, `query_emails` and `list_inbox_emails` includes three ready-to-use link fields alongside the raw `message_id`:

| Field | Example |
|---|---|
| `message_url` | `message:%3C0123456789abcdef0123456789abcdef@example.com%3E` |
| `mid_url` | `mid:0123456789abcdef0123456789abcdef@example.com` |
| `markdown_link` | `[Weekly Status Report](mid:0123456789abcdef0123456789abcdef@example.com)` |

All three are `null` when a message has no Message-ID. `message_url` is percent-encoded, because a literal `%` in a Message-ID would otherwise break the `message:` scheme. `mid_url` is left bare per [RFC 2392](https://datatracker.ietf.org/doc/html/rfc2392). `markdown_link` uses the scheme set by `MAILMATE_LINK_SCHEME`, strips `[` and `]` from the subject, and falls back to the encoded `message:` target when the bare `mid:` form contains parentheses that would end the Markdown link early.

## Memory and freshness

ChromaDB holds the whole vector index in RAM while it's loaded, which is several GB for a large mailbox. The server releases the index and models after `MCP_IDLE_UNLOAD_SECONDS` (default 10 minutes) without a call, then reloads them in a few seconds on the next one. After an `index` run or any other command that changes the index, the server reloads automatically on its next call, so results never lag behind the last run. With `MCP_INDEX_INTERVAL_SECONDS` set, the server does the indexing itself and needs no reload; see [Index inside the MCP server](scheduling.md#index-inside-the-mcp-server).

## Claude Desktop (macOS)

Claude Desktop reads MCP servers from:

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

Add a `mail-semantic-search` entry under `mcpServers`, replace `/Users/you/mail-semantic-search` with your project folder, then fully quit and reopen Claude Desktop.

### Option A: local venv (recommended)

```json
{
  "mcpServers": {
    "mail-semantic-search": {
      "command": "/bin/zsh",
      "args": [
        "-lc",
        "cd /Users/you/mail-semantic-search && .venv/bin/mail-semantic-search-mcp"
      ]
    }
  }
}
```

The `cd` matters: `.env` is loaded from the working directory. `.venv/bin/python -m mail_semantic_search.mcp_server` works too.

### Option B: MCP via Docker

Runs the server in the same container setup as `docker compose run ... index`, so the MCP client never opens SQLite or ChromaDB on the host. Use this when the client is blocked from your data folder (for example on `/Volumes/...`) and you'd rather not set up the HTTP transport. `-i` keeps stdin open for MCP; `-T` disables the pseudo-TTY.

```json
{
  "mcpServers": {
    "mail-semantic-search": {
      "command": "/bin/zsh",
      "args": [
        "-lc",
        "cd /Users/you/mail-semantic-search && docker compose run --rm -i -T --entrypoint python mail-semantic-search -m mail_semantic_search.mcp_server"
      ]
    }
  }
}
```

Docker Desktop must be running, and the first tool call may wait for the container to start.

If Option A shows zero indexed emails while Docker search works, your host `.env` paths don't match the Compose mount. Align them or use Option B.

## Claude Code

```bash
claude mcp add --scope user mail-semantic-search -- /bin/zsh -lc "cd /Users/you/mail-semantic-search && .venv/bin/mail-semantic-search-mcp"
```

`--scope user` makes it available in every project. Leave it off to register the server for the current project only.

## Other clients

Any MCP client that can launch a stdio server works. Point it at the same command as Option A: run `.venv/bin/mail-semantic-search-mcp` from the project folder.

## MCP over HTTP (Full Disk Access workaround)

macOS Full Disk Access is **not** inherited by processes an app spawns. If your maildir is on an external volume and your MCP client isn't itself allowed to read it, every `.eml` read fails with `Operation not permitted`. Granting access to a spawned subprocess is unreliable.

The workaround is to run the server on its own over HTTP from a process that **does** have Full Disk Access (a terminal, Keyboard Maestro or launchd), and connect the client through a stdio-to-HTTP bridge.

**1. Start the HTTP server** from a terminal with Full Disk Access:

```bash
MCP_TRANSPORT=http .venv/bin/mail-semantic-search-mcp
```

It prints `Mail Semantic Search MCP listening on http://127.0.0.1:6543/mcp` and binds to loopback only.

To start it at login, use a Keyboard Maestro macro with an **At Login** trigger and an **Execute Shell Script** action:

```sh
#!/bin/bash
PORT="${MCP_PORT:-6543}"
LOG="$HOME/Library/Logs/mailmate-search-mcp.log"
DIR="/Users/you/mail-semantic-search"
mkdir -p "$(dirname "$LOG")"
# Idempotent: don't spawn a duplicate.
if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "$(date '+%F %T') MCP already listening on $PORT, skipping" >> "$LOG"
  exit 0
fi
echo "$(date '+%F %T') starting MCP on port $PORT" >> "$LOG"
cd "$DIR" || exit 1
MCP_TRANSPORT=http MCP_PORT="$PORT" \
  nohup "$DIR/.venv/bin/mail-semantic-search-mcp" >> "$LOG" 2>&1 &
disown
```

You can add a second **Periodic** trigger (every 5 minutes) that runs the same script. The `lsof` check makes repeat runs harmless, and the server comes back if it ever dies.

To have this server keep the index current too, add `MCP_INDEX_INTERVAL_SECONDS=300` next to `MCP_TRANSPORT=http` and remove any separate scheduled index job. See [Index inside the MCP server](scheduling.md#index-inside-the-mcp-server).

**2. Point Claude Desktop at it through a bridge.** Claude Desktop doesn't speak HTTP MCP natively; it silently ignores a bare `"url"` field. Use `mcp-remote`:

```json
{
  "mcpServers": {
    "mail-semantic-search": {
      "command": "/Users/you/.nvm/versions/node/v22.20.0/bin/npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:6543/mcp"]
    }
  }
}
```

Use the absolute path to `npx`. Claude Desktop doesn't inherit your shell `PATH`, so a plain `"npx"` won't resolve when Node is installed through nvm.

The bridge only relays JSON-RPC and never touches the filesystem, so it doesn't need Full Disk Access. If you'd rather avoid npm, `pip install mcp-proxy` and use `mcp-proxy` in place of `npx mcp-remote`.

Clients that support HTTP MCP natively (Claude Code included: `claude mcp add --transport http mail-semantic-search http://127.0.0.1:6543/mcp`) can connect without a bridge.
