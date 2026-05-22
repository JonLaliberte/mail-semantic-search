"""FastMCP server exposing mail-semantic-search tools."""

import logging
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
    """Search emails using semantic retrieval with optional metadata filters."""
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
    """Query emails using metadata filters only."""
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
    """Return indexing status and configuration summary."""
    return get_status_data_payload()


@mcp.tool
def list_inbox_emails(
    limit: int = 50,
    account: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
) -> dict:
    """List emails currently in any IMAP INBOX, newest first."""
    return list_inbox_emails_payload(
        InboxRequest(
            limit=limit,
            account=account,
            date_after=_parse_mcp_date(date_after),
            date_before=_parse_mcp_date(date_before),
        )
    )


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
        """Open the given email in MailMate. Accepts the RFC-822 Message-ID with or without angle brackets."""
        return _open_email(message_id)

    @mcp.tool
    def mark_email_read(message_id: str) -> dict:
        """Mark the given email as read in MailMate (sets the IMAP \\Seen flag)."""
        return _mark_email_read(message_id)

    @mcp.tool
    def archive_email(message_id: str) -> dict:
        """Archive the given email in MailMate (invokes MailMate's archive: action)."""
        return _archive_email(message_id)

    @mcp.tool
    def mark_read_and_archive(message_id: str) -> dict:
        """Mark as read AND archive in a single MailMate round-trip — use when finishing triage of one email."""
        return _mark_read_and_archive(message_id)


def main() -> None:
    """Run the MCP server over stdio."""
    configure_logging()
    configure_runtime_diagnostics()
    logger.info(
        "MCP startup paths: chromadb_path=%s database_path=%s email_dir=%s",
        config.chromadb_path,
        config.database_path,
        config.email_dir,
    )
    try:
        status = get_status_data_payload()
        logger.info(
            "MCP startup index: total_indexed_emails=%s total_emails=%s",
            status.get("total_indexed_emails"),
            status.get("total_emails"),
        )
    except Exception as exc:
        hint = (
            "This is usually the MCP *parent app* (not this repo) blocking access to the configured "
            "database path—for example SQLite on `/Volumes/...` while Docker uses the same files "
            "fine. Same single dataset: run MCP inside `docker compose` (see README: MCP via Docker) "
            "or grant Full Disk Access to the MCP client."
        )
        msg = (
            f"MCP startup: could not open the index for a status snapshot ({type(exc).__name__}: {exc}). "
            f"The server will still start; search/get_status may fail until the process can open the DB. "
            f"{hint}"
        )
        logger.warning(msg)
        print(msg, file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
