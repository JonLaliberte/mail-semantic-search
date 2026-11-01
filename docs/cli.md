# CLI reference

Run commands from the project folder so `.env` is picked up:

```bash
.venv/bin/mail-semantic-search <command> [options]
# or, with Docker:
docker compose run --rm mail-semantic-search <command> [options]
```

`mail-semantic-search --help` and `<command> --help` list every option. `--version` prints the installed version.

## Indexing

### `index`

Index emails from `EMAIL_DIR`. Already-indexed, unchanged files are skipped, so you can stop a run and start it again later.

| Option | Description |
|---|---|
| `--incremental` | Only scan files changed since the last successful incremental run. Use this for scheduled runs. |
| `--no-skip` | Re-index every email even if already indexed (full rebuild). Needed after changing `EMBEDDING_MODEL`. |
| `--limit N` | Index at most N emails (for testing). |

How `--incremental` works:

- The primary watermark is the time of the last successful incremental scan. Older indexes that predate the watermark fall back to the newest indexed file's mtime.
- It scans files whose filesystem change time (ctime) is newer than `watermark - INCREMENTAL_OVERLAP_SECONDS` (default 24 hours). It uses ctime rather than mtime because MailMate moves a message between mailboxes (for example, archiving a Gmail message from Inbox to All Mail) by renaming the file, which keeps the old mtime. Moved messages are picked up on the next run and their index entry is repointed to the new path.
- The watermark only advances after a fully successful run.
- Writes are keyed by file path and vector ID, so re-running is safe.

### `index-file <path>`

Index a single `.eml` end to end: parse, check for duplicates, embed, write. It's built for MailMate rules that fire on new mail, so a message becomes searchable immediately instead of waiting for the next scheduled run:

```bash
mail-semantic-search index-file "$MM_PATH"
```

| Option | Description |
|---|---|
| `--force` | Re-embed and re-write even if the stored mtime matches. |

## Searching

### `search "query"`

Meaning-based search, optionally narrowed by metadata filters.

```bash
mail-semantic-search search "flight confirmation for the Denver trip"
mail-semantic-search search "signed contract" --from legal@ --attachment-type pdf --date-after 2024-01-01
```

| Option | Description |
|---|---|
| `--from` | Sender, partial match. |
| `--to` | Recipient (To, CC or BCC), partial match. |
| `--subject` | Subject, exact match. |
| `--subject-like` | Subject, partial match. |
| `--date-after`, `--date-before` | Date bounds (`YYYY-MM-DD`). |
| `--has-attachments`, `--no-attachments` | Only emails with / without attachments. |
| `--attachment-type` | Attachment extension, e.g. `pdf`, `jpg`. |
| `--attachment-name` | Attachment filename, partial match. |
| `--show-attachments` | Show attachment details in results. |
| `--auto-filters/--no-auto-filters` | Override `QUERY_PARSER_ENABLED` for this query. |
| `--rerank/--no-rerank` | Override `RERANK_ENABLED` for this query. |

### `query`

Metadata-only lookup: no embedding, so it's fast. It takes the same filter options as `search` (without a query string), plus:

| Option | Description |
|---|---|
| `--limit N` | Max results. Defaults to, and is capped at, `MAX_FILTERED_SEARCH_LIMIT` (1000). |

```bash
mail-semantic-search query --from "billing@" --has-attachments --date-after 2025-01-01
```

## Inspecting

### `status`

Index size, configuration and version.

### `inspect --file-path "/full/path/to/email.eml"`

Show the stored SQLite metadata and the exact document stored in the vector index for one email. Useful when a message isn't showing up the way you expect.

## Attachments for AI clients

### `stage`

Copy an indexed email's attachments and `.eml` to `STAGING_DIR/<short-hash>/` (default `~/Documents/mailmate-staged`). Use this when an MCP client's filesystem sandbox can't read the original file, for example a maildir on an external drive the client has no Full Disk Access for.

```bash
mail-semantic-search stage --file-path "/path/to.eml"
mail-semantic-search stage --message-id "<abc123@example.com>"
```

| Option | Description |
|---|---|
| `--no-eml` | Copy attachments only. |

The same email always stages to the same folder.

### `clear-staged`

Delete staged copies. `--short-hash X` removes one; with no option it removes all of them. Source emails and the index are never touched.

## Maintenance

All destructive maintenance commands take `--dry-run`. Run that first.

### `prune`

Remove index entries whose `.eml` file no longer exists. Deleted or moved mail otherwise leaves orphaned rows behind. It scans the mail folder once and deletes orphaned rows and their vectors. As a safety check, it aborts if the mail folder is missing or the scan finds zero files, so an unmounted drive can't wipe the index. `--batch-size N` sets the delete batch size (default 1000).

### `dedup`

Remove duplicate index entries that share a `Message-ID`, keeping the most recently indexed copy.

### `compact-vectors`

Rebuild the vector index to reclaim memory. ChromaDB holds its index in RAM and never reuses the slots of deleted vectors, so `prune`, `dedup` and `migrate-paths` leave dead entries behind. A full path migration doubles the index. This command copies live vectors into a fresh collection and swaps it in, reusing the existing embeddings.

- `--dry-run` shows the current size and the expected size after compaction.
- Before the real run, stop the MCP server and back up the data folder. Restart the server afterwards.
- Needs free disk space of about twice the current `chromadb` folder while it runs.
- Concurrent `index` runs skip cleanly while it holds the lock. An interrupted run resumes when you run it again.
- `--batch-size N` sets vectors copied per batch (default 1000).

### `reextract`

Re-parse and re-embed already-indexed emails with the current extractor. It's needed only when a release changes how emails are parsed; the release notes will say so.

- **One email** (prints a before/after diff of the preview): `reextract --file-path "/path/to.eml"` or `reextract --message-id "<...>"`
- **Bulk** (every row produced by an older extractor): `reextract`, with optional `--limit N`, `--batch-size 64`, `--dry-run`

Bulk runs are resumable. While one is running, scheduled `index --incremental` calls print a benign "Backfill in progress … skipping this incremental run" line and exit 0. The line doesn't contain "error" or "warning", so job watchers don't raise alerts.

### `migrate-paths`

Rewrite stored file paths after your maildir moves, reusing existing embeddings:

```bash
mail-semantic-search migrate-paths --old-prefix "/old/path/" --new-prefix "/new/path/" --dry-run
```

Back up the data folder first. Afterwards, run `compact-vectors` to reclaim the memory the migration used.
