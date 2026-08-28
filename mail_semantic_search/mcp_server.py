"""FastMCP server exposing mail-semantic-search tools.

Default transport is stdio (spawned by an MCP client). On macOS, when the
parent MCP client lacks Full Disk Access, anything reading from external
volumes blows up with EACCES — Apple does not inherit FDA across the spawn.
To work around that, run this server standalone over HTTP from a terminal
that DOES have FDA:

    MCP_TRANSPORT=http mail-semantic-search-mcp

…then point your MCP client at `http://127.0.0.1:6543/mcp` instead of
letting it spawn the process. Defaults:

    MCP_TRANSPORT  stdio | http       (default: stdio)
    MCP_HOST       bind address       (default: 127.0.0.1, loopback only)
    MCP_PORT       TCP port           (default: 6543)
    MCP_PATH       URL path           (default: /mcp)
"""

import logging
import os
import platform
import sys
from datetime import datetime
from typing import Optional

from fastmcp import FastMCP

from mail_semantic_search.config import config
from mail_semantic_search.runtime_logging import (
    configure_logging,
    configure_runtime_diagnostics,
)
from mail_semantic_search.search import (
    get_status_data_payload,
    list_inbox_emails_payload,
    query_email_records_payload,
    search_email_records_payload,
)
from mail_semantic_search.service_models import InboxRequest, QueryRequest, SearchRequest
from mail_semantic_search.staging import clear_staged, stage_email

logger = logging.getLogger(__name__)

mcp = FastMCP(name="Mail Semantic Search")


def _parse_mcp_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse optional ISO date string for MCP tool inputs."""
    if not date_str:
        return None
    return datetime.fromisoformat(date_str)


def _resolve_has_attachments(
    has_attachments: Optional[bool],
    no_attachments: Optional[bool],
) -> Optional[bool]:
    """Normalize mutually exclusive attachment filter flags."""
    if has_attachments and no_attachments:
        raise ValueError(
            "Choose either has_attachments or no_attachments, not both."
        )
    if has_attachments is True:
        return True
    if no_attachments is True:
        return False
    return None


@mcp.tool
def search_emails(
    query: str,
    from_addr: Optional[str] = None,
    to_addr: Optional[str] = None,
    subject: Optional[str] = None,
    subject_like: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    has_attachments: Optional[bool] = None,
    no_attachments: Optional[bool] = None,
    attachment_type: Optional[str] = None,
    attachment_name: Optional[str] = None,
    limit: Optional[int] = None,
    auto_filters: Optional[bool] = None,
    rerank: Optional[bool] = None,
) -> dict:
    """Rank indexed emails by semantic similarity, with optional metadata filters.

    Use this tool for concepts, topics, or wording that may not appear verbatim
    in a message. For sender, recipient, subject, date, or attachment lookup
    without a content-ranking question, use `query_emails` instead; it is
    faster and does not load the embedding or reranking models.

    All explicit filters are combined with AND. To find messages sent to OR
    from one address, call the appropriate tool twice and deduplicate the
    combined results by `message_id`. The response includes the effective
    query, applied filters, parser/reranker flags, and similarity-ranked
    results with message links, metadata, body previews, and attachments.

    Args:
        query: Natural-language description of the email content to find.
        from_addr: Case-insensitive partial match against the sender name or address.
        to_addr: Case-insensitive partial match across To, CC, and BCC recipients.
        subject: Exact match against the complete stored subject.
        subject_like: Case-insensitive partial match within the subject.
        date_after: Inclusive lower date bound as an ISO-8601 date or timestamp.
        date_before: Inclusive upper date bound as an ISO-8601 date or timestamp.
        has_attachments: Set true to require at least one attachment.
        no_attachments: Set true to require no attachments; mutually exclusive with has_attachments.
        attachment_type: Exact file extension, with or without a leading dot, such as `pdf` or `.jpg`.
        attachment_name: Case-insensitive partial match within attachment filenames.
        limit: Maximum number of final ranked results; defaults to the configured search result count.
        auto_filters: True enables natural-language query rewriting and filter extraction; false disables it; null uses configuration.
        rerank: True enables local cross-encoder reranking; false keeps vector ranking; null uses configuration.
    """
    return search_email_records_payload(
        SearchRequest(
            query=query,
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            subject_like=subject_like,
            date_after=_parse_mcp_date(date_after),
            date_before=_parse_mcp_date(date_before),
            has_attachments=_resolve_has_attachments(has_attachments, no_attachments),
            attachment_type=attachment_type,
            attachment_name=attachment_name,
            limit=limit,
            auto_filters=auto_filters,
            rerank=rerank,
        )
    )


@mcp.tool
def query_emails(
    from_addr: Optional[str] = None,
    to_addr: Optional[str] = None,
    subject: Optional[str] = None,
    subject_like: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    has_attachments: Optional[bool] = None,
    no_attachments: Optional[bool] = None,
    attachment_type: Optional[str] = None,
    attachment_name: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """List indexed emails matching metadata filters, newest first.

    Use this tool when sender, recipient, subject, date, or attachment metadata
    answers the request. Use `search_emails` instead when results must be ranked
    by the meaning of their content. All supplied filters are combined with
    AND. To find messages sent to OR from one address, call this tool twice and
    deduplicate the combined results by `message_id`.

    Results are capped (see `limit` in the response for the cap actually
    applied). If `truncated` is true more emails matched than were returned —
    narrow the filters or continue with an earlier `date_before`. Results
    include message links, metadata, body previews, file paths, and attachment
    details; this tool does not read attachment bytes.

    Args:
        from_addr: Case-insensitive partial match against the sender name or address.
        to_addr: Case-insensitive partial match across To, CC, and BCC recipients.
        subject: Exact match against the complete stored subject.
        subject_like: Case-insensitive partial match within the subject.
        date_after: Inclusive lower date bound as an ISO-8601 date or timestamp.
        date_before: Inclusive upper date bound as an ISO-8601 date or timestamp.
        has_attachments: Set true to require at least one attachment.
        no_attachments: Set true to require no attachments; mutually exclusive with has_attachments.
        attachment_type: Exact file extension, with or without a leading dot, such as `pdf` or `.jpg`.
        attachment_name: Case-insensitive partial match within attachment filenames.
        limit: Maximum rows to return; null uses MAX_FILTERED_SEARCH_LIMIT and larger values are capped there.
    """
    return query_email_records_payload(
        QueryRequest(
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            subject_like=subject_like,
            date_after=_parse_mcp_date(date_after),
            date_before=_parse_mcp_date(date_before),
            has_attachments=_resolve_has_attachments(has_attachments, no_attachments),
            attachment_type=attachment_type,
            attachment_name=attachment_name,
            limit=limit,
        )
    )


@mcp.tool
def get_status() -> dict:
    """Check whether the email index is available and report its configuration.

    Returns SQLite and vector-store email counts, attachment statistics, date
    range, configured paths, embedding model, batch size, and default search
    result count. This is read-only and does not scan or update email files.
    """
    return get_status_data_payload()


@mcp.tool
def list_inbox_emails(
    limit: int = 50,
    account: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
) -> dict:
    """List messages currently in MailMate IMAP INBOX folders, newest first.

    Inbox membership is determined from the indexed file path, so archived or
    moved messages are excluded. This is a metadata listing, not semantic
    search. Results include a short body snippet and message links. For paging,
    pass the oldest result's date as `date_before`; inbox date bounds are strict
    so that boundary message is not repeated.

    Args:
        limit: Number of messages to return; defaults to 50 and is clamped to 1-500.
        account: Optional bare account email address, such as `user@example.com`; null includes every indexed account.
        date_after: Exclusive lower date bound as an ISO-8601 date or timestamp.
        date_before: Exclusive upper date bound as an ISO-8601 date or timestamp.
    """
    return list_inbox_emails_payload(
        InboxRequest(
            limit=limit,
            account=account,
            date_after=_parse_mcp_date(date_after),
            date_before=_parse_mcp_date(date_before),
        )
    )


@mcp.tool
def stage_email_attachments(
    message_id: Optional[str] = None,
    file_path: Optional[str] = None,
    include_eml: bool = True,
) -> dict:
    """Copy an indexed email's attachment bytes to a sandbox-accessible directory.

    Use after finding a message when attachment metadata is insufficient and
    the actual PDF, image, document, or other bytes must be read. The source
    `.eml` often lives on an external
    volume the MCP client cannot access; this stages a per-email copy under
    ~/Documents/mailmate-staged/<hash>/ where Read tools can reach it.

    Pass either message_id (with or without angle brackets) or file_path.
    Idempotent — same email always stages to the same dir; calling again
    refreshes the contents. The response contains absolute staged paths for the
    `.eml` and each extracted attachment; it does not return attachment bytes.

    Args:
        message_id: RFC-822 Message-ID from a search result, with or without angle brackets; provide this or file_path.
        file_path: Exact indexed source `.eml` path from a search result; provide this or message_id.
        include_eml: Also copy the complete source message as `message.eml`; attachments are always extracted.
    """
    if not message_id and not file_path:
        return {"status": "failed", "message": "Pass message_id or file_path"}
    return stage_email(file_path=file_path, message_id=message_id, include_eml=include_eml)


@mcp.tool
def clear_staged_emails(short_hash: Optional[str] = None) -> dict:
    """Delete temporary copies created by `stage_email_attachments`.

    This removes only staged copies, never source emails or the search index.
    Pass a `short_hash` to remove one staged message. Omitting it deletes every
    staged message directory under the configured staging root.

    Args:
        short_hash: Twelve-character staged directory name; null clears all staged message directories.
    """
    return clear_staged(short_hash=short_hash)


# macOS-only MailMate actions. Registered only when running on Darwin so the
# rest of the server still works on Linux (Docker images, etc.).
if platform.system() == "Darwin":
    from mail_semantic_search.mailmate_actions import (
        archive_email as _archive_email,
        mark_email_read as _mark_email_read,
        mark_read_and_archive as _mark_read_and_archive,
        open_email as _open_email,
    )

    @mcp.tool
    def open_email(message_id: str) -> dict:
        """Open one indexed message in MailMate and bring the app to the foreground.

        This causes a visible UI action but does not modify the message.

        Args:
            message_id: RFC-822 Message-ID from a search result, with or without angle brackets.
        """
        return _open_email(message_id)

    @mcp.tool
    def mark_email_read(message_id: str) -> dict:
        """Mark one message as read in MailMate by setting its IMAP \\Seen flag.

        This is a state-changing action. Use it only when the user asks to
        change the message's read state.

        Args:
            message_id: RFC-822 Message-ID from a search result, with or without angle brackets.
        """
        return _mark_email_read(message_id)

    @mcp.tool
    def archive_email(message_id: str) -> dict:
        """Archive one message using MailMate's configured archive action.

        This is a state-changing action that can move the message out of its
        current mailbox. Use it only when the user asks to archive that message.

        Args:
            message_id: RFC-822 Message-ID from a search result, with or without angle brackets.
        """
        return _archive_email(message_id)

    @mcp.tool
    def mark_read_and_archive(message_id: str) -> dict:
        """Mark one message read and archive it in a single MailMate action.

        This is a state-changing action that performs both operations. Use it
        only when the user asks to finish triage by marking that message read
        and archiving it.

        Args:
            message_id: RFC-822 Message-ID from a search result, with or without angle brackets.
        """
        return _mark_read_and_archive(message_id)


def _resolve_transport() -> tuple[str, dict]:
    """Read MCP_TRANSPORT env (and friends) into a (transport, kwargs) pair.

    Stdio stays the backwards-compatible default. HTTP is the recommended
    mode on macOS for clients that spawn the MCP under a sandbox that lacks
    Full Disk Access — start this server from a terminal that has FDA and
    point the client at the URL instead.
    """
    transport = (os.getenv("MCP_TRANSPORT") or "stdio").lower()
    if transport not in {"stdio", "http", "sse", "streamable-http"}:
        raise SystemExit(
            f"Unknown MCP_TRANSPORT={transport!r}. "
            "Use 'stdio' (default), 'http' (recommended for macOS FDA workaround), "
            "or 'sse' / 'streamable-http' for older clients."
        )
    if transport == "stdio":
        return transport, {}

    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "6543"))
    path = os.getenv("MCP_PATH", "/mcp")
    return transport, {"host": host, "port": port, "path": path}


def _log_startup_status_async() -> None:
    """Best-effort: fetch the index status on a daemon thread.

    On a 400k+ row collection this takes ~8s, which used to block the
    listener from starting. Moving it off the critical path means HTTP
    binds immediately and the diagnostic still lands in the log.
    """
    import threading

    def _probe() -> None:
        try:
            status = get_status_data_payload()
            logger.info(
                "MCP startup index: total_indexed_emails=%s total_emails=%s",
                status.get("total_indexed_emails"),
                status.get("total_emails"),
            )
        except Exception as exc:
            hint = (
                "This is usually the MCP *parent app* (not this repo) blocking access to the "
                "configured database path—for example SQLite on `/Volumes/...` while Docker uses "
                "the same files fine. Same single dataset: run MCP inside `docker compose` "
                "(see README: MCP via Docker), grant Full Disk Access, or use the HTTP transport "
                "(MCP_TRANSPORT=http) and run this from a terminal that has FDA."
            )
            msg = (
                f"MCP startup: could not open the index for a status snapshot "
                f"({type(exc).__name__}: {exc}). The server will still serve; search/get_status "
                f"may fail until the process can open the DB. {hint}"
            )
            logger.warning(msg)
            print(msg, file=sys.stderr)

    threading.Thread(target=_probe, name="mcp-startup-status", daemon=True).start()


def main() -> None:
    """Run the MCP server. Defaults to stdio; HTTP via MCP_TRANSPORT=http."""
    configure_logging()
    configure_runtime_diagnostics()
    logger.info(
        "MCP startup paths: chromadb_path=%s database_path=%s email_dir=%s",
        config.chromadb_path,
        config.database_path,
        config.email_dir,
    )

    transport, kwargs = _resolve_transport()
    if transport != "stdio":
        host = kwargs["host"]
        port = kwargs["port"]
        path = kwargs["path"]
        startup_msg = (
            f"Mail Semantic Search MCP listening on http://{host}:{port}{path}  "
            f"(transport={transport})"
        )
        logger.info(startup_msg)
        # Print to stderr so a terminal launch shows the URL immediately.
        print(startup_msg, file=sys.stderr)

    _log_startup_status_async()
    mcp.run(transport=transport, **kwargs)


if __name__ == "__main__":
    main()
