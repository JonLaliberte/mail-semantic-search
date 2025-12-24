"""Indexing logic for emails."""

import os
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from mailmate_search.config import config
from mailmate_search.embedding_service import EmbeddingService
from mailmate_search.mailmate_reader import read_emails_batch
from mailmate_search.vector_store import VectorStore


def combine_email_text(email: dict) -> str:
    """Combine email fields into a single text for embedding."""
    subject = email.get("subject", "")
    body = email.get("body", "")[:2000]  # Limit body length
    from_addr = email.get("from", "")
    return f"{subject}\n{from_addr}\n{body}".strip()


def index_emails(
    limit: Optional[int] = None,
    skip_indexed: bool = True,
    show_progress: bool = True,
) -> None:
    """Index all emails from the MailMate directory."""
    email_dir = config.mailmate_email_dir

    if not email_dir.exists():
        print(f"Error: MailMate email directory not found: {email_dir}")
        print("Please set MAILMATE_EMAIL_DIR in your .env file")
        return

    print(f"Indexing emails from: {email_dir}")
    print(f"Using embedding model: {config.embedding_model}")
    print(f"Batch size: {config.batch_size}")

    # Initialize services
    embedding_service = EmbeddingService()
    vector_store = VectorStore()

    # Get stats before indexing
    stats_before = vector_store.get_stats()
    print(f"Already indexed: {stats_before['total_emails']} emails")

    total_indexed = 0
    total_skipped = 0

    # Process emails in batches
    batch_iter = read_emails_batch(
        email_dir, batch_size=config.batch_size, show_progress=show_progress
    )

    if show_progress:
        pbar = tqdm(desc="Indexing emails", unit="emails")

    try:
        for batch in batch_iter:
            if limit and total_indexed >= limit:
                break

            # Filter out already indexed emails if requested
            emails_to_index = []
            if skip_indexed:
                for email in batch:
                    if not vector_store.is_indexed(email["file_path"]):
                        emails_to_index.append(email)
                    else:
                        total_skipped += 1
            else:
                emails_to_index = batch

            if not emails_to_index:
                continue

            # Combine email text for embedding
            texts = [combine_email_text(email) for email in emails_to_index]

            # Generate embeddings
            embeddings = embedding_service.embed_texts(texts)

            # Store in vector database
            vector_store.add_emails(emails_to_index, embeddings)

            total_indexed += len(emails_to_index)

            if show_progress:
                pbar.update(len(emails_to_index))
                pbar.set_postfix(
                    {
                        "indexed": total_indexed,
                        "skipped": total_skipped,
                    }
                )

    except KeyboardInterrupt:
        print("\nIndexing interrupted by user")
    finally:
        if show_progress:
            pbar.close()

    # Get final stats
    stats_after = vector_store.get_stats()
    print(f"\nIndexing complete!")
    print(f"Newly indexed: {total_indexed} emails")
    print(f"Skipped (already indexed): {total_skipped} emails")
    print(f"Total indexed: {stats_after['total_emails']} emails")

