"""CLI interface for mail-semantic-search."""

import logging
import sqlite3
import sys
from datetime import datetime
from typing import Optional, Tuple

import click

from mail_semantic_search.database import Database
from mail_semantic_search.index import index_emails
from mail_semantic_search.vector_store import VectorStore
from mail_semantic_search.runtime_logging import (
    configure_logging,
    configure_runtime_diagnostics,
    get_runtime_log_path,
)
from mail_semantic_search.search import (
    display_indexed_email,
    display_results,
    get_indexed_email_data,
    get_status_data,
    query_email_records,
    search_emails,
)
from mail_semantic_search.service_models import QueryRequest

logger = logging.getLogger(__name__)


# Issue #13: Standardized error handling helpers
def parse_date(date_str: Optional[str], option_name: str) -> Tuple[Optional[datetime], Optional[str]]:
    """Parse a date string, returning (parsed_date, error_message).
    
    Returns (None, None) if date_str is None.
    Returns (datetime, None) on success.
    Returns (None, error_message) on failure.
    """
    if not date_str:
        return None, None
    try:
        return datetime.fromisoformat(date_str), None
    except ValueError:
        return None, f"Invalid date format for {option_name}: {date_str}. Use YYYY-MM-DD format."


def handle_error(
    message: str,
    exit_code: int = 1,
    *,
    log_exception: bool = False,
) -> None:
    """Display an error message and exit."""
    if log_exception:
        logger.exception(message)
    click.echo(f"Error: {message}", err=True)
    if log_exception:
        click.echo(f"Details logged to: {get_runtime_log_path()}", err=True)
    sys.exit(exit_code)


@click.group()
@click.version_option(version="0.1.0")
def main():
    """mail-semantic-search - Semantic search for local email files.

    Common examples:
      index --incremental   Scan files newer than the incremental watermark minus overlap
      index --no-skip       Re-index all emails even if already indexed
      search "quarterly planning deck"   Find relevant emails by meaning
    """
    configure_logging()
    configure_runtime_diagnostics()


@main.command()
@click.option(
    "--limit",
    type=int,
    help="Limit the number of emails to index (for testing)",
)
@click.option(
    "--no-skip",
    is_flag=True,
    help="Re-index all emails even if already indexed",
)
@click.option(
    "--incremental",
    is_flag=True,
    help="Only scan files newer than the saved incremental watermark minus overlap",
)
def index(limit: int, no_skip: bool, incremental: bool):
    """Index emails from a local directory."""
    try:
        index_emails(
            limit=limit,
            skip_indexed=not no_skip,
            show_progress=True,
            incremental=incremental,
        )
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Indexing failed: {e}", log_exception=True)


@main.command()
@click.argument("query", required=True)
@click.option("--from", "from_addr", help="Filter by sender (partial match)")
@click.option("--to", "to_addr", help="Filter by recipient (partial match)")
@click.option("--subject", help="Filter by exact subject")
@click.option("--subject-like", help="Filter by subject (partial match)")
@click.option("--date-after", help="Filter by date after (YYYY-MM-DD)")
@click.option("--date-before", help="Filter by date before (YYYY-MM-DD)")
@click.option("--has-attachments", is_flag=True, help="Only show emails with attachments")
@click.option("--no-attachments", is_flag=True, help="Only show emails without attachments")
@click.option("--attachment-type", help="Filter by attachment file extension (e.g., pdf, jpg)")
@click.option("--attachment-name", help="Filter by attachment filename (partial match)")
@click.option("--show-attachments", is_flag=True, help="Show attachment details in results")
@click.option(
    "--auto-filters/--no-auto-filters",
    default=None,
    help="Enable/disable local natural-language filter extraction",
)
@click.option(
    "--rerank/--no-rerank",
    default=None,
    help="Enable/disable local cross-encoder reranking",
)
def search(
    query: str,
    from_addr: Optional[str],
    to_addr: Optional[str],
    subject: Optional[str],
    subject_like: Optional[str],
    date_after: Optional[str],
    date_before: Optional[str],
    has_attachments: bool,
    no_attachments: bool,
    attachment_type: Optional[str],
    attachment_name: Optional[str],
    show_attachments: bool,
    auto_filters: Optional[bool],
    rerank: Optional[bool],
):
    """Search for emails using natural language query with optional filters."""
    # Issue #13: Standardized date parsing with consistent error messages
    parsed_date_after, error = parse_date(date_after, "--date-after")
    if error:
        handle_error(error)
    
    parsed_date_before, error = parse_date(date_before, "--date-before")
    if error:
        handle_error(error)
    
    # Handle attachment filter
    has_attachments_flag = None
    if has_attachments:
        has_attachments_flag = True
    elif no_attachments:
        has_attachments_flag = False
    
    try:
        search_emails(
            query,
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            subject_like=subject_like,
            date_after=parsed_date_after,
            date_before=parsed_date_before,
            has_attachments=has_attachments_flag,
            attachment_type=attachment_type,
            attachment_name=attachment_name,
            show_attachments=show_attachments,
            auto_filters=auto_filters,
            rerank=rerank,
        )
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Search failed: {e}", log_exception=True)


@main.command()
@click.option("--from", "from_addr", help="Filter by sender (partial match)")
@click.option("--to", "to_addr", help="Filter by recipient (partial match)")
@click.option("--subject", help="Filter by exact subject")
@click.option("--subject-like", help="Filter by subject (partial match)")
@click.option("--date-after", help="Filter by date after (YYYY-MM-DD)")
@click.option("--date-before", help="Filter by date before (YYYY-MM-DD)")
@click.option("--has-attachments", is_flag=True, help="Only show emails with attachments")
@click.option("--no-attachments", is_flag=True, help="Only show emails without attachments")
@click.option("--attachment-type", help="Filter by attachment file extension (e.g., pdf, jpg)")
@click.option("--attachment-name", help="Filter by attachment filename (partial match)")
@click.option("--limit", type=int, help="Limit number of results")
@click.option("--show-attachments", is_flag=True, help="Show attachment details")
def query(
    from_addr: Optional[str],
    to_addr: Optional[str],
    subject: Optional[str],
    subject_like: Optional[str],
    date_after: Optional[str],
    date_before: Optional[str],
    has_attachments: bool,
    no_attachments: bool,
    attachment_type: Optional[str],
    attachment_name: Optional[str],
    limit: Optional[int],
    show_attachments: bool,
):
    """Query emails using metadata filters (no semantic search)."""
    # Issue #13: Standardized date parsing with consistent error messages
    parsed_date_after, error = parse_date(date_after, "--date-after")
    if error:
        handle_error(error)
    
    parsed_date_before, error = parse_date(date_before, "--date-before")
    if error:
        handle_error(error)
    
    # Handle attachment filter
    has_attachments_flag = None
    if has_attachments:
        has_attachments_flag = True
    elif no_attachments:
        has_attachments_flag = False
    
    try:
        response = query_email_records(
            QueryRequest(
                from_addr=from_addr,
                to_addr=to_addr,
                subject=subject,
                subject_like=subject_like,
                date_after=parsed_date_after,
                date_before=parsed_date_before,
                has_attachments=has_attachments_flag,
                attachment_type=attachment_type,
                attachment_name=attachment_name,
                limit=limit,
            )
        )

        # Display results
        display_results(response.results, show_attachments=show_attachments)
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Query failed: {e}", log_exception=True)


@main.command()
@click.option(
    "--file-path",
    required=True,
    help="Exact email file path to inspect in the index",
)
def inspect(file_path: str):
    """Show indexed SQLite and Chroma data for one email."""
    try:
        data = get_indexed_email_data(file_path)
        if not data:
            handle_error(f"No indexed email found for file path: {file_path}")
        display_indexed_email(data)
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Inspect failed: {e}", log_exception=True)


@main.command()
def status():
    """Show indexing status and statistics."""
    try:
        status_data = get_status_data()

        print("Semantic Search Status")
        print("=" * 40)
        print(f"Embedding Model: {status_data.embedding_model}")
        print(f"Email Directory: {status_data.email_directory}")
        print(f"ChromaDB Path: {status_data.chromadb_path}")
        print(f"Database Path: {status_data.database_path}")
        print(f"\nChromaDB Statistics:")
        print(f"  Total Indexed Emails: {status_data.total_indexed_emails}")
        print(f"\nDatabase Statistics:")
        print(f"  Total Emails: {status_data.total_emails}")
        print(f"  Total Attachments: {status_data.total_attachments}")
        print(f"  Emails with Attachments: {status_data.emails_with_attachments}")
        if status_data.date_range["min"]:
            print(
                f"  Date Range: {status_data.date_range['min']} to {status_data.date_range['max']}"
            )
        print(f"\nConfiguration:")
        print(f"  Batch Size: {status_data.batch_size}")
        print(f"  Search Results: {status_data.search_results}")
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Failed to get status: {e}", log_exception=True)


@main.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report duplicates without deleting anything",
)
def dedup(dry_run: bool):
    """Remove duplicate index entries for the same Message-ID.

    Keeps the most recently indexed copy and deletes all others from both
    SQLite and ChromaDB. Rows with no Message-ID are left untouched.

    Safe to run multiple times (idempotent).
    """
    try:
        with Database() as database, VectorStore() as vector_store:
            if dry_run:
                groups, total_dupes = database.count_duplicate_message_ids()
                click.echo(
                    f"Dry run: {groups} message_ids have duplicates, "
                    f"{total_dupes} rows would be removed."
                )
                return

            click.echo("Scanning for duplicate message_ids...")
            removed, kept = database.dedup_by_message_id(vector_store)
            click.echo(f"Done. Removed {removed} duplicate(s), kept {kept} unique message_id(s).")
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Dedup failed: {e}", log_exception=True)


if __name__ == "__main__":
    main()

