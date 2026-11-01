# mail-semantic-search

Find any email you've ever received by describing it. "The lease renewal the landlord sent last spring." "Anything about the roof leak." "Invoices with a PDF attached from 2023." mail-semantic-search builds a private search index of your MailMate archive on your own machine. You can then search it by meaning, filter by sender, date and attachments, or let Claude and other AI assistants search it for you. Nothing leaves your computer.

## What you get

- **Search by meaning, not exact words.** A question about "travel plans" finds the flight confirmation that never uses the word "travel". Attachment text (PDF, Word, Excel, PowerPoint) is indexed too.
- **Precise filters when you know them.** Sender, recipient, subject, date range, attachment type or filename. Combine them with a meaning-based query, or use them on their own for instant lookups.
- **Your inbox, available to your AI assistant.** A built-in [MCP](https://modelcontextprotocol.io) server lets Claude Desktop, Claude Code and other MCP clients search your mail, list your inbox, open messages in MailMate, and mark them read or archive them.
- **Private by design.** Embedding models run locally. No API keys, no cloud, no telemetry.
- **Stays current cheaply.** Incremental runs only look at mail that changed since the last run, which typically takes 10–30 seconds.

## Requirements

- macOS (Linux works for search and indexing; the MailMate actions are macOS only)
- [MailMate](https://freron.com), or any folder of `.eml` files
- Python 3.10+ (or Docker)
- 16 GB RAM recommended; about 2.5 GB of disk per 35 GB of mail

## Install

### Let your AI assistant do it

Paste this into Claude Code, Cursor, Codex or another coding agent that can run commands:

```text
Install and set up mail-semantic-search for me by following the instructions at
https://raw.githubusercontent.com/JonLaliberte/mail-semantic-search/main/llms-install.md
Check with me before anything slow or anything that changes system settings.
```

The agent checks your Python version, finds your MailMate folder, installs everything, runs a test index, connects the MCP server to your AI tools, and offers to schedule automatic updates. It asks before each long-running step.

### Or do it yourself

```bash
git clone https://github.com/JonLaliberte/mail-semantic-search.git
cd mail-semantic-search
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env    # then set EMAIL_DIR to your MailMate Messages folder (absolute path, no ~)
```

Build the index. The first run downloads the embedding model (about 420 MB) and can take several hours on a large archive. You can stop it and restart it at any point; it resumes where it left off.

```bash
.venv/bin/mail-semantic-search index
```

Search:

```bash
.venv/bin/mail-semantic-search search "the contract renewal we discussed in March"
.venv/bin/mail-semantic-search query --from "accounting@" --has-attachments --date-after 2024-01-01
```

To use Docker instead of a local Python environment, see [Installation](docs/installation.md#docker).

## Use it from Claude

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`, using the folder you cloned into, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "mail-semantic-search": {
      "command": "/bin/zsh",
      "args": ["-lc", "cd /Users/you/mail-semantic-search && .venv/bin/mail-semantic-search-mcp"]
    }
  }
}
```

For Claude Code:

```bash
claude mcp add --scope user mail-semantic-search -- /bin/zsh -lc "cd /Users/you/mail-semantic-search && .venv/bin/mail-semantic-search-mcp"
```

Now you can ask things like *"What did the contractor say about the permit timeline?"* or *"Show me unread inbox mail from this week and archive the newsletters."*

If your mail is on an external drive, see [MCP server](docs/mcp.md) for the macOS Full Disk Access workaround.

## Keep it up to date

Run `index --incremental` on a schedule (hourly works well) so new mail becomes searchable. [Scheduling](docs/scheduling.md) has ready-to-use launchd, cron and Keyboard Maestro setups.

## Documentation

| Guide | What's in it |
|---|---|
| [Installation](docs/installation.md) | Native and Docker setup, disk and memory sizing, upgrading |
| [Configuration](docs/configuration.md) | Every `.env` setting: models, reranking, query parsing, logging |
| [CLI reference](docs/cli.md) | All commands: indexing, search, maintenance |
| [MCP server](docs/mcp.md) | Tools, filters, deep links, Claude Desktop setup, HTTP transport |
| [Scheduling](docs/scheduling.md) | Keeping the index fresh with launchd, cron or Keyboard Maestro |
| [Troubleshooting](docs/troubleshooting.md) | Common errors and how to fix them |

## How it works

Each message is parsed (with quoted replies removed and attachment text extracted) and turned into a vector by a local [sentence-transformers](https://www.sbert.net) model (BGE-base by default). The vectors go into [ChromaDB](https://www.trychroma.com) and the metadata into SQLite. A search embeds your query the same way and returns the closest messages, narrowed by any filters. A local cross-encoder can optionally re-rank the results for higher precision.

## License

MIT
