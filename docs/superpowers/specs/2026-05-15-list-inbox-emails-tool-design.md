# `list_inbox_emails` MCP tool

## Goal

Expose an MCP tool that returns a list of emails currently in any IMAP `INBOX` folder, with enough metadata to skim without opening the message. Inbox-only filtering is keyed off the file path, which encodes the mailbox folder (no schema change required).

## Background

MailMate stores messages on disk under paths like:

```
.../MailMate/Messages/IMAP/<account-url-encoded>/INBOX.mailbox/Messages/4.eml
.../MailMate/Messages/IMAP/<account-url-encoded>/[Gmail].mailbox/All Mail.mailbox/4.eml   ← archived
```

The `INBOX.mailbox/` segment is the reliable inbox marker. Account is encoded in the path (e.g. `brainstormenterprises%40gmail.com@imap.gmail.com`).

The DB's `body_preview` column is already populated at index time (truncated to `config.body_preview_limit`, default 2000 chars), so a shorter snippet is a simple substring.

## Tool surface

```python
@mcp.tool
def list_inbox_emails(
    limit: int = 50,
    account: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
) -> dict
```

- `limit` — default 50, hard cap 500.
- `account` — optional. Accepts bare form (`"brainstormenterprises@gmail.com"`); implementation URL-encodes `@` → `%40` before matching the path. Omitted ⇒ all accounts.
- `date_after` / `date_before` — optional ISO date strings (`YYYY-MM-DD` or full ISO datetime). Bounds are strict: `date > date_after` and `date < date_before`. Strict `<` lets a caller page through history by feeding the oldest result's date back in as `date_before`.

## Per-row payload

```json
{
  "id": 123,
  "message_id": "<...>",
  "from": "...",
  "to": "...",
  "subject": "...",
  "date": "2026-05-15T...",
  "has_attachments": true,
  "body_snippet": "first ~200 chars of body_preview..."
}
```

- `body_snippet` is the existing `body_preview` truncated to 200 chars (Python char slice — UTF-8-safe at the codepoint level). `NULL`/empty `body_preview` → `""`.
- `has_attachments` is cast to a real `bool` (SQLite stores `0`/`1`).
- `from` / `to` map from DB columns `from_addr` / `to_addrs` to keep the JSON shape close to the existing `_normalize_result` convention.

## Response shape

Mirrors `QueryResponse`:

```json
{
  "filters": {
    "account": "...",
    "limit": 50,
    "date_after": null,
    "date_before": "2026-05-15T..."
  },
  "results": [ /* rows */ ]
}
```

### Pagination usage

Caller wanting older results: take the last row's `date` from the current batch and pass it as `date_before` on the next call. Repeat until `results` is empty. Boundary case — multiple rows sharing the exact same `date` value can drop out of paged results because `<` is strict; in practice mail timestamps are second-precision and collisions are rare. If a caller really needs perfect coverage they can dedupe across batches by `message_id`.

## SQL

```sql
SELECT id, message_id, from_addr, to_addrs, subject, date,
       has_attachments, body_preview
FROM emails
WHERE file_path LIKE '%/INBOX.mailbox/%'
  -- AND file_path LIKE '%<account-encoded>%/INBOX.mailbox/%'  (if account given)
  -- AND date > ?                                              (if date_after given)
  -- AND date < ?                                              (if date_before given)
ORDER BY date DESC
LIMIT ?
```

No schema migration. No new index — the existing schema does not index `file_path`, but the row counts are bounded by inbox size in practice; if this becomes slow we can add an index later.

## Code layout

- `mail_semantic_search/service_models.py` — add `InboxRequest`, `InboxResponse` dataclasses.
- `mail_semantic_search/database.py` — add `Database.list_inbox_emails(limit, account)` returning a list of dicts.
- `mail_semantic_search/search.py` — add `list_inbox_emails_payload(request) -> dict` wrapper that opens a `Database`, calls the method, builds the response, and returns `asdict(...)`.
- `mail_semantic_search/mcp_server.py` — add `@mcp.tool list_inbox_emails(...)`.

## Validation

- `limit` clamped to `1..500`. Values outside the range are clamped (not rejected), matching the relaxed-input style used elsewhere in the MCP layer.
- `account` is not validated for format. If it doesn't match any path, the result is just empty — that's the right behavior for an unknown account.
- `date_after` / `date_before` parsed via `datetime.fromisoformat`. Malformed strings raise `ValueError` and surface to the caller (consistent with how the other MCP tools handle `_parse_mcp_date`).

## Tests (`tests/test_inbox.py`)

1. Returns only rows whose `file_path` contains `/INBOX.mailbox/`.
2. Excludes archived rows under `[Gmail].mailbox/All Mail.mailbox`.
3. `account` filter: passing the bare email matches the URL-encoded path; omitting it returns rows from all accounts.
4. `body_snippet` is ≤ 200 chars even when `body_preview` is longer.
5. Sorted by `date DESC`, capped at `limit`.
6. `limit` clamping: 0 or negative → at least 1 row; >500 → no more than 500 rows.
7. `date_before` strict — a row whose `date` equals `date_before` is excluded.
8. `date_after` strict — a row whose `date` equals `date_after` is excluded.
9. Paging round-trip: first call returns N rows; second call with `date_before=<last row's date>` returns the next page with no overlap.

## Out of scope

- No "full body" tool — punted until a need shows up.
- No per-folder filter beyond inbox. Other mailboxes can be added later if the pattern proves out.
- No new `file_path` index — defer until query timing actually warrants it.
