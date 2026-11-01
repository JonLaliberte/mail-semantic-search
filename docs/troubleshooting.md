# Troubleshooting

Internal warnings and tracebacks go to `LOG_PATH` (default `./data/logs/mail-semantic-search.error.log`), not the terminal. Check there first.

## Indexing

**Email directory not found / zero files indexed**
- Check that `EMAIL_DIR` in `.env` is an absolute path to the folder holding your `.eml` files. The MailMate default is `~/Library/Application Support/MailMate/Messages`, written out in full in `.env`.
- Run commands from the project folder; `.env` is loaded from the current directory.

**`Operation not permitted` reading mail**
- macOS is blocking access. Give Full Disk Access to the app running the command (Terminal, iTerm, Keyboard Maestro, or `/bin/zsh` for launchd jobs). This is common when the maildir is on an external drive.

**Out of memory**
- Lower `BATCH_SIZE` in `.env`.
- Use a smaller model such as `BGE-small-en-v1.5`. Switching models requires a full re-index.

**Indexing is slow**
- A first full index of a large archive (35 GB, ~700k messages) takes several hours. That's expected.
- You can stop it at any time. Already-indexed emails are skipped when you run it again.
- Use `--incremental` for routine runs, and `--no-skip` only when you want a full rebuild.

**`Cannot send a request, as the client has been closed`**
- Fixed after v0.9.0. Older versions re-checked model files against Hugging Face on every load, so a Hub outage could hang indexing and then kill it with this error. Upgrade; models now load from `MODEL_CACHE_DIR` without touching the network.
- The very first run still downloads the models, so it needs access to `huggingface.co`.

**"Backfill in progress" / "Indexing already in progress" … "skipping this incremental run"**
- Another long job (`reextract`, `compact-vectors` or an earlier `index` run) holds the index lock. Scheduled runs skip cleanly until it finishes, and the lock clears on its own if that process dies. Nothing is wrong.

## MCP server

**The server uses several GB of memory**
- That's the vector index, which ChromaDB keeps entirely in RAM while loaded. The server releases it after `MCP_IDLE_UNLOAD_SECONDS` (default 10 minutes) of inactivity.
- If `compact-vectors --dry-run` shows the index is much bigger than its expected size, run `compact-vectors` to drop dead vectors.

**Claude shows zero indexed emails, but CLI search works**
- The MCP server is reading a different data folder. Make sure the configured command `cd`s into the project folder, so it picks up the same `.env`.
- If you index with Docker, your host `.env` paths may not match the Compose mount. Align them or use [MCP via Docker](mcp.md#option-b-mcp-via-docker).

**`unable to open database file`, or ChromaDB errors, with data on `/Volumes/...`**
- The MCP client app (the parent process) is blocked from that path even though your terminal or Docker isn't. Pick one fix:
  - Run the server over HTTP from a process with Full Disk Access ([MCP over HTTP](mcp.md#mcp-over-http-full-disk-access-workaround)).
  - Run it [inside Docker](mcp.md#option-b-mcp-via-docker).
  - Grant Full Disk Access to the client app.
  - Move the data folder under your home directory.

**`PermissionError` writing `LOG_PATH` on an external volume**
- Different apps on macOS have different file access, so a log path Docker can write may be off-limits to the MCP client. Point `LOG_PATH` somewhere inside the project or your home folder. If `LOG_PATH` isn't writable, the app falls back to `./data/logs`, then the system temp directory, and warns once.

**Claude Desktop ignores the server**
- Fully quit and reopen Claude Desktop after editing the config.
- Claude Desktop doesn't support a bare `"url"` entry. Use a stdio command, or a bridge for HTTP ([details](mcp.md#mcp-over-http-full-disk-access-workaround)).
- Use absolute paths. Claude Desktop doesn't inherit your shell's `PATH`.
