# `open_email_url` MCP tool

## Goal

Expose an MCP tool that opens a `message://` URL on the host, so an MCP client can deep-link an indexed search result back into the user's mail client.

## Behavior

- **Name:** `open_email_url`
- **Input:** `message_url: str` — a `message://` URL.
- **Action:** `subprocess.run(["open", message_url], capture_output=True, text=True)`.
- **Success:** return `{"opened": message_url}`.
- **Failure:** non-zero `returncode` → raise `RuntimeError` whose message includes the return code and `stderr` text.

The macOS `open` command resolves the URL to its registered handler. No application is named explicitly — whatever app owns `message://` (typically MailMate or Mail.app) is launched. No `activate` step.

## What this is not

- No URL validation. If the scheme is wrong or no handler is registered, `open` will exit non-zero and we surface the error.
- No platform fallback. macOS-only; that matches the rest of this project (it already depends on MailMate).
- No "select message" beyond what the URL itself encodes.

## Files touched

- `mail_semantic_search/mcp_server.py` — add `open_email_url` tool alongside the existing `search_emails` / `query_emails` / `get_status` tools.
- `tests/test_mcp_server.py` (new) — unit tests that patch `subprocess.run` to cover the command shape, the success payload, and the `RuntimeError` branch.

## Testing

The test patches `subprocess.run` so it never actually shells out. Three cases:

1. Success path — assert the `["open", url]` args, `capture_output=True`, `text=True`, and the `{"opened": url}` return.
2. Failure path — `returncode=1`, `stderr="…"` → `RuntimeError` raised, stderr surfaced in the message.
3. The tool is registered on the FastMCP instance (sanity check).
