# Installing mail-semantic-search (instructions for AI agents)

You are helping a user install mail-semantic-search: a local semantic search index over their email (MailMate `.eml` files) with a CLI and an MCP server. Follow these steps in order. Run the commands yourself where you can. Explain what you're doing in plain language, because the user may not be technical.

Ground rules:
- **Ask before** anything slow (the full index can take hours), anything that changes system settings (Full Disk Access, launchd jobs, MCP client configs), and before overwriting an existing file.
- Never read, print or summarize the contents of the user's emails beyond what's needed to verify the install works. Do not send any email content anywhere.
- Everything runs locally. No API keys are needed.
- Full documentation: https://github.com/JonLaliberte/mail-semantic-search/tree/main/docs

## 1. Check prerequisites

- OS: macOS is the main target. Linux works for indexing, search and MCP; the MailMate actions are macOS only.
- Python 3.10 or newer: `python3 --version`. If it's missing or too old on macOS, offer `brew install python@3.12` (Homebrew) and use that interpreter (e.g. `/opt/homebrew/bin/python3.12`) in step 3.
- `git --version`.
- Memory: 16 GB+ RAM recommended (`sysctl -n hw.memsize` on macOS). Note it to the user if lower, but continue.

## 2. Locate the mail folder

The default MailMate location is `~/Library/Application Support/MailMate/Messages`. Confirm it exists and contains `.eml` files:

```bash
find -L "$HOME/Library/Application Support/MailMate/Messages" -name '*.eml' -print -quit
```

If nothing is found, ask the user where their mail is stored. It may be on an external drive under `/Volumes/...`, or a symlink. Count the messages (`find -L "<dir>" -name '*.eml' | wc -l`) so you can estimate disk use (about 2.5 GB per 700k messages) and, after the smoke test in step 4, indexing time.

If `find` reports `Operation not permitted`, the terminal app needs Full Disk Access: System Settings → Privacy & Security → Full Disk Access. Ask the user to grant it, then restart the terminal session.

## 3. Install

Ask the user where to put the project (suggest `~/mail-semantic-search`). Below, `INSTALL_DIR` stands for that absolute path. Quote it in shell commands if it contains spaces. Then:

```bash
git clone https://github.com/JonLaliberte/mail-semantic-search.git INSTALL_DIR
cd INSTALL_DIR
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

Edit `.env` and set `EMAIL_DIR` to the **absolute** mail folder path from step 2. Expand `~` to the full home path, and keep any spaces as-is with no quotes. Leave the other settings at their defaults unless the user asks otherwise.

All commands must run from `INSTALL_DIR`, because `.env` is loaded from the current working directory.

## 4. Smoke test

Index a small sample. The first run downloads the embedding model (~420 MB) from huggingface.co.

```bash
.venv/bin/mail-semantic-search index --limit 300
.venv/bin/mail-semantic-search status
.venv/bin/mail-semantic-search search "invoice"
```

Time the `index --limit 300` run and extrapolate to the full message count for the estimate in step 5 (exclude the one-off model download). Confirm `status` shows indexed emails and `search` returns results. Show the user only the subjects and senders, not the bodies. If something fails, check `data/logs/mail-semantic-search.error.log` and https://github.com/JonLaliberte/mail-semantic-search/blob/main/docs/troubleshooting.md.

## 5. Full index (ask first)

Tell the user how long you estimate it will take, then ask whether to start it now. It's safe to interrupt; re-running resumes and skips finished messages. Run it in the background so it survives this session:

```bash
cd INSTALL_DIR && nohup .venv/bin/mail-semantic-search index > data/index-full.log 2>&1 &
```

The user can check progress with `tail -f INSTALL_DIR/data/index-full.log`.

## 6. Connect AI clients (ask which ones)

The MCP server command is the same for every client:

```
/bin/zsh -lc "cd INSTALL_DIR && .venv/bin/mail-semantic-search-mcp"
```

**Claude Code:**

```bash
claude mcp add --scope user mail-semantic-search -- /bin/zsh -lc "cd INSTALL_DIR && .venv/bin/mail-semantic-search-mcp"
```

**Claude Desktop:** merge this into `~/Library/Application Support/Claude/claude_desktop_config.json`. Keep any existing `mcpServers` entries, and back the file up first. Then tell the user to fully quit and reopen Claude Desktop.

```json
{
  "mcpServers": {
    "mail-semantic-search": {
      "command": "/bin/zsh",
      "args": ["-lc", "cd INSTALL_DIR && .venv/bin/mail-semantic-search-mcp"]
    }
  }
}
```

**Other MCP clients** (Cursor, Windsurf, etc.): register a stdio server with the same command and arguments.

If the mail folder or `INSTALL_DIR` is on an external drive (`/Volumes/...`), macOS may block the client app from reading it. Explain this to the user and point them to the HTTP workaround: https://github.com/JonLaliberte/mail-semantic-search/blob/main/docs/mcp.md#mcp-over-http-full-disk-access-workaround

## 7. Schedule updates (ask first)

Offer an hourly launchd job so new mail stays searchable. Create `~/Library/LaunchAgents/com.mail-semantic-search.index.plist`:

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
        <string>cd INSTALL_DIR &amp;&amp; .venv/bin/mail-semantic-search index --incremental</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>StandardOutPath</key>
    <string>/tmp/mail-semantic-search-index.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mail-semantic-search-index.error.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

Replace `INSTALL_DIR` with the real path. Escape `&` as `&amp;` in the plist if the path contains one. Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.mail-semantic-search.index.plist
```

Don't start the scheduled job while the full index from step 5 is still running for the first time. Incremental runs skip while another index run holds the lock, so overlapping is harmless, just pointless.

On Linux, offer a cron entry instead: `0 * * * * cd INSTALL_DIR && .venv/bin/mail-semantic-search index --incremental >> /tmp/mail-semantic-search-index.log 2>&1`.

## 8. Wrap up

Tell the user:
- Where it's installed, and where the index lives (`INSTALL_DIR/data`).
- Whether the full index is running and how to check it.
- Which AI clients you connected, and that they should restart those apps.
- That they can try asking their assistant something like "find the email where someone sent me a receipt for a flight".
- How to upgrade later: `cd INSTALL_DIR && git pull && .venv/bin/pip install -e .`
