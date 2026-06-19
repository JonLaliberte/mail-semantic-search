"""CLI interface for mail-semantic-search."""

import logging
import sqlite3
import sys
from datetime import datetime
from typing import List, Optional, Tuple

import click

from mail_semantic_search.database import Database, get_file_hash
from mail_semantic_search.index import index_email_file, index_emails
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
from mail_semantic_search.version import resolve_commit, resolve_version

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
@click.version_option(
    version=resolve_version(),
    message=f"%(prog)s %(version)s (commit {resolve_commit()})",
)
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


@main.command("index-file")
@click.argument("file_path", type=click.Path(dir_okay=False))
@click.option(
    "--force",
    is_flag=True,
    help="Re-embed and re-upsert even if the stored mtime matches",
)
def index_file(file_path: str, force: bool):
    """Index a single .eml file (e.g. from a MailMate rule on new mail)."""
    from pathlib import Path

    try:
        result = index_email_file(Path(file_path), force=force)
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
        return
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Indexing failed: {e}", log_exception=True)
        return

    click.echo(f"{result['status']}: {result['message']}")
    if result["status"] in ("not_found", "failed"):
        sys.exit(1)


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
        print(f"Version: {resolve_version()} (commit {resolve_commit()})")
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


@main.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report how many entries would be pruned without deleting anything",
)
@click.option(
    "--batch-size",
    type=int,
    default=1000,
    help="Rows deleted per commit batch (default: 1000)",
)
def prune(dry_run: bool, batch_size: int):
    """Remove index entries whose .eml file no longer exists on disk.

    Scans the mail directory once, then deletes any SQLite row (and its
    matching ChromaDB vector) whose file_path is gone. This reconciles the
    common case where emails were deleted or moved in MailMate but their index
    rows lingered — the SQLite table never prunes vanished files on its own —
    bringing the SQLite and ChromaDB counts back into agreement.

    Safe to run multiple times (idempotent).
    """
    from pathlib import Path

    from mail_semantic_search.config import config
    from mail_semantic_search.mailmate_reader import scan_eml_files

    email_dir = config.email_dir
    if not Path(email_dir).exists():
        handle_error(
            f"Mail directory not found: {email_dir}. Refusing to prune — an "
            "unavailable directory would look like every email was deleted."
        )
        return

    click.echo(f"Scanning for current .eml files in: {email_dir}")
    present_paths = {str(p) for p in scan_eml_files(email_dir, show_progress=False)}

    if not present_paths:
        handle_error(
            f"No .eml files found under {email_dir}. Refusing to prune — this "
            "usually means the drive is unmounted or the path is wrong, not "
            "that every indexed email is stale."
        )
        return

    click.echo(f"Found {len(present_paths)} emails on disk.")

    try:
        with Database() as database, VectorStore() as vector_store:
            if dry_run:
                missing, present = database.count_missing_files(present_paths)
                click.echo(
                    f"Dry run: {missing} orphaned row(s) would be pruned, "
                    f"{present} kept."
                )
                return

            click.echo("Pruning index entries with no file on disk...")
            removed, kept = database.prune_missing_files(
                vector_store, present_paths=present_paths, batch_size=batch_size
            )
            click.echo(f"Done. Pruned {removed} orphaned row(s); kept {kept}.")
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Prune failed: {e}", log_exception=True)


@main.command()
@click.option(
    "--file-path",
    help="Reextract one email by its exact file path",
)
@click.option(
    "--message-id",
    help="Reextract one email by Message-ID",
)
@click.option(
    "--limit",
    type=int,
    help="Bulk mode: cap the number of stale rows processed",
)
@click.option(
    "--batch-size",
    type=int,
    default=64,
    help="Bulk mode: rows per embed/upsert batch (default: 64)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Bulk mode: count stale rows without writing",
)
def reextract(
    file_path: Optional[str],
    message_id: Optional[str],
    limit: Optional[int],
    batch_size: int,
    dry_run: bool,
):
    """Re-parse and re-embed indexed emails using the current extractor.

    Two modes:

      Single-email (visual QA): pass --file-path or --message-id. Prints a
      before/after body_preview diff so you can verify the new extractor on
      one row before kicking off a bulk pass.

      Bulk backfill: no selector. Walks every row where extraction_version <
      CURRENT_EXTRACTION_VERSION and processes them in batches. Holds the
      backfill lock for the whole run so concurrent `index --incremental`
      runs cleanly no-op (KM-safe).

    Resumable: completed rows have their extraction_version bumped to the
    current value, so interrupting and re-running picks up where it left off.
    """
    from mail_semantic_search.index import _reextract_single, _reextract_bulk

    selectors = [bool(file_path), bool(message_id)]
    if any(selectors):
        if all(selectors):
            handle_error("Pass at most one of --file-path or --message-id.")
        try:
            _reextract_single(file_path=file_path, message_id=message_id)
        except sqlite3.Error as e:
            handle_error(f"Database error: {e}", log_exception=True)
        except (OSError, RuntimeError, ValueError, TypeError) as e:
            handle_error(f"Reextract failed: {e}", log_exception=True)
        return

    try:
        _reextract_bulk(limit=limit, batch_size=batch_size, dry_run=dry_run)
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Reextract failed: {e}", log_exception=True)


@main.command()
@click.option("--file-path", help="Stage an email by its indexed file path")
@click.option("--message-id", help="Stage an email by Message-ID")
@click.option("--no-eml", is_flag=True, help="Skip copying the .eml itself (attachments only)")
def stage(file_path: Optional[str], message_id: Optional[str], no_eml: bool):
    """Copy an indexed email's attachments + .eml to ~/Documents/mailmate-staged/."""
    from mail_semantic_search.staging import stage_email

    if not file_path and not message_id:
        handle_error("Pass --file-path or --message-id.")

    try:
        result = stage_email(
            file_path=file_path,
            message_id=message_id,
            include_eml=not no_eml,
        )
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
        return
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Stage failed: {e}", log_exception=True)
        return

    click.echo(f"{result['status']}: {result['message']}")
    if result.get("staged_dir"):
        click.echo(f"  dir: {result['staged_dir']}")
    if result.get("eml_path"):
        click.echo(f"  eml: {result['eml_path']}")
    for a in result.get("attachments", []):
        click.echo(f"  attachment ({a['size']} bytes, {a['content_type']}): {a['path']}")


@main.command("clear-staged")
@click.option("--short-hash", help="Clear a single staged email by its short hash; omit to clear all")
def clear_staged_cmd(short_hash: Optional[str]):
    """Remove staged email directories created by `stage`."""
    from mail_semantic_search.staging import clear_staged

    try:
        result = clear_staged(short_hash=short_hash)
    except (OSError, ValueError) as e:
        handle_error(f"Clear failed: {e}", log_exception=True)
        return

    click.echo(f"{result['status']}: {result['message']}")


@main.command("migrate-paths")
@click.option("--old-prefix", required=True, help="Path prefix to rewrite (e.g. /emails/)")
@click.option("--new-prefix", required=True, help="Replacement prefix (e.g. /Volumes/External Storage SSD/MailMate/Messages/)")
@click.option("--batch-size", type=int, default=500, help="Rows per Chroma batch")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing")
def migrate_paths(old_prefix: str, new_prefix: str, batch_size: int, dry_run: bool):
    """Rewrite indexed file_path values from one prefix to another.

    Migrates both SQLite (emails.file_path, emails.file_hash) and ChromaDB
    (document IDs are md5(file_path), and metadata['file_path']). Reuses
    existing embeddings — no re-embed cost.

    Idempotent: rows whose new-id already exists in Chroma are skipped, so
    interrupted runs can be re-run safely.
    """
    if not old_prefix.endswith("/"):
        old_prefix = old_prefix + "/"
    if not new_prefix.endswith("/"):
        new_prefix = new_prefix + "/"

    try:
        with Database() as database, VectorStore() as vector_store:
            cursor = database.conn.cursor()
            cursor.execute(
                "SELECT id, file_path FROM emails WHERE file_path LIKE ? ORDER BY id",
                (f"{old_prefix}%",),
            )
            rows = cursor.fetchall()
            total = len(rows)
            click.echo(f"Found {total} rows with prefix {old_prefix!r}")
            if total == 0:
                click.echo("Nothing to migrate.")
                return

            if dry_run:
                click.echo("Sample migrations (first 3):")
                for r in rows[:3]:
                    old = r["file_path"]
                    new = new_prefix + old[len(old_prefix):]
                    click.echo(f"  {old}")
                    click.echo(f"   → {new}")
                return

            collection = vector_store.collection
            migrated = 0
            already_migrated = 0
            orphan_in_chroma = 0
            sqlite_updates: List[Tuple[str, str, int]] = []

            for batch_start in range(0, total, batch_size):
                batch = rows[batch_start : batch_start + batch_size]
                old_paths = [r["file_path"] for r in batch]
                new_paths = [new_prefix + p[len(old_prefix):] for p in old_paths]
                old_ids = [get_file_hash(p) for p in old_paths]
                new_ids = [get_file_hash(p) for p in new_paths]

                # Idempotency: which new_ids already exist in Chroma?
                existing_new = collection.get(ids=new_ids, include=[])
                already_set = set(existing_new.get("ids") or [])

                # Build the to-migrate list
                pending_indices = [
                    i for i in range(len(batch))
                    if new_ids[i] not in already_set
                ]
                already_migrated += len(batch) - len(pending_indices)

                if pending_indices:
                    pending_old_ids = [old_ids[i] for i in pending_indices]
                    existing_old = collection.get(
                        ids=pending_old_ids,
                        include=["embeddings", "metadatas", "documents"],
                    )
                    old_by_id = {
                        existing_old["ids"][k]: k
                        for k in range(len(existing_old.get("ids") or []))
                    }

                    upsert_ids: List[str] = []
                    upsert_embeddings = []
                    upsert_metadatas = []
                    upsert_documents = []
                    delete_ids: List[str] = []

                    for i in pending_indices:
                        if old_ids[i] not in old_by_id:
                            orphan_in_chroma += 1
                            # Still need to update SQLite for consistency
                            sqlite_updates.append((new_paths[i], new_ids[i], batch[i]["id"]))
                            continue
                        k = old_by_id[old_ids[i]]
                        meta = dict(existing_old["metadatas"][k] or {})
                        meta["file_path"] = new_paths[i]
                        upsert_ids.append(new_ids[i])
                        upsert_embeddings.append(existing_old["embeddings"][k])
                        upsert_metadatas.append(meta)
                        upsert_documents.append(existing_old["documents"][k])
                        delete_ids.append(old_ids[i])
                        sqlite_updates.append((new_paths[i], new_ids[i], batch[i]["id"]))

                    if upsert_ids:
                        collection.upsert(
                            ids=upsert_ids,
                            embeddings=upsert_embeddings,
                            metadatas=upsert_metadatas,
                            documents=upsert_documents,
                        )
                        collection.delete(ids=delete_ids)
                        migrated += len(upsert_ids)

                # Rows already-migrated in Chroma still need SQLite update if
                # we got interrupted between Chroma+SQLite updates last time
                for i in range(len(batch)):
                    if new_ids[i] in already_set:
                        sqlite_updates.append((new_paths[i], new_ids[i], batch[i]["id"]))

                if (batch_start // batch_size) % 5 == 0:
                    click.echo(
                        f"  Progress: {batch_start + len(batch)}/{total} "
                        f"(chroma migrated={migrated}, already={already_migrated}, "
                        f"orphan={orphan_in_chroma})"
                    )

            # Bulk SQLite update — single transaction
            click.echo(f"Updating SQLite for {len(sqlite_updates)} rows...")
            cursor.executemany(
                "UPDATE emails SET file_path = ?, file_hash = ? WHERE id = ?",
                sqlite_updates,
            )
            database.conn.commit()

            click.echo(
                f"Done. Chroma migrated={migrated}, "
                f"already-migrated={already_migrated}, "
                f"orphans={orphan_in_chroma}, "
                f"SQLite rows updated={len(sqlite_updates)}."
            )
    except sqlite3.Error as e:
        handle_error(f"Database error: {e}", log_exception=True)
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        handle_error(f"Migration failed: {e}", log_exception=True)


if __name__ == "__main__":
    main()

