"""FastMCP server exposing MailMate search tools."""

from datetime import datetime
from typing import Optional

from fastmcp import FastMCP

from mailmate_search.runtime_logging import (
    configure_logging,
    configure_runtime_diagnostics,
)
from mailmate_search.search import (
    get_status_data_payload,
    query_email_records_payload,
    search_email_records_payload,
)
from mailmate_search.service_models import QueryRequest, SearchRequest

mcp = FastMCP(name="MailMate Search")


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


def main() -> None:
    """Run the MCP server over stdio."""
    configure_logging()
    configure_runtime_diagnostics()
    mcp.run()


if __name__ == "__main__":
    main()
