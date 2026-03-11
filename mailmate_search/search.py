"""Search logic for emails."""

from datetime import datetime
from typing import List, Optional

from mailmate_search.config import config
from mailmate_search.database import Database, get_file_hash
from mailmate_search.embedding_service import EmbeddingService
from mailmate_search.query_parser import LocalQueryParser
from mailmate_search.query import QueryBuilder
from mailmate_search.reranker import CrossEncoderReranker
from mailmate_search.vector_store import VectorStore


def format_date(date_str: str) -> str:
    """Format date string for display."""
    if not date_str:
        return "Unknown date"
    try:
        # Try parsing ISO format
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return date_str


def display_results(results: List[dict], show_attachments: bool = False) -> None:
    """Display search results in a readable format."""
    if not results:
        print("No results found.")
        return

    print(f"\nFound {len(results)} results:\n")
    print("=" * 80)

    # Issue #14: Use config constants for display limits
    max_attachments_display = config.MAX_ATTACHMENTS_DISPLAY
    max_preview_length = config.MAX_PREVIEW_LENGTH

    for i, result in enumerate(results, 1):
        score = result.get("similarity", result.get("distance", 0))
        if isinstance(score, (int, float)) and score < 1:
            score = 1 - score  # Convert distance to similarity
        if isinstance(score, (int, float)):
            header = f"\n[{i}] Similarity: {score:.3f}"
        else:
            header = f"\n[{i}]"

        rerank_score = result.get("rerank_score")
        if isinstance(rerank_score, (int, float)):
            header += f" | Rerank: {rerank_score:.3f}"
        print(header)
        print(f"From: {result.get('from', result.get('from_addr', 'Unknown'))}")
        print(f"Subject: {result.get('subject', 'No subject')}")
        print(f"Date: {format_date(str(result.get('date', '')))}")
        print(f"File: {result.get('file_path', 'Unknown')}")
        
        # Show attachments if available
        attachments = result.get("attachments", [])
        if attachments and show_attachments:
            print(f"Attachments ({len(attachments)}):")
            for att in attachments[:max_attachments_display]:
                filename = att.get("filename", "Unknown")
                size = att.get("size", 0)
                size_str = f" ({size:,} bytes)" if size > 0 else ""
                print(f"  - {filename}{size_str}")
            if len(attachments) > max_attachments_display:
                print(f"  ... and {len(attachments) - max_attachments_display} more")
        
        if result.get("document"):
            preview = result["document"][:max_preview_length]
            print(f"Preview: {preview}...")
        print("-" * 80)


def _parse_optional_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse YYYY-MM-DD or ISO datetime, returning None on invalid."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        return None


def search_emails(
    query: str,
    from_addr: Optional[str] = None,
    to_addr: Optional[str] = None,
    subject: Optional[str] = None,
    subject_like: Optional[str] = None,
    date_after: Optional[datetime] = None,
    date_before: Optional[datetime] = None,
    has_attachments: Optional[bool] = None,
    attachment_type: Optional[str] = None,
    attachment_name: Optional[str] = None,
    show_attachments: bool = False,
    auto_filters: Optional[bool] = None,
    rerank: Optional[bool] = None,
) -> None:
    """Search for emails matching a query with optional filters."""
    print(f"Searching for: '{query}'")
    print(f"Using embedding model: {config.embedding_model}")

    parser_enabled = config.query_parser_enabled if auto_filters is None else auto_filters
    rerank_enabled = config.rerank_enabled if rerank is None else rerank
    effective_query = query

    if parser_enabled:
        parser = LocalQueryParser()
        parsed_query = parser.parse(query)
        if parsed_query:
            effective_query = parsed_query.semantic_query

            if from_addr is None:
                from_addr = parsed_query.from_addr
            if to_addr is None:
                to_addr = parsed_query.to_addr
            if subject is None:
                subject = parsed_query.subject
            if subject_like is None:
                subject_like = parsed_query.subject_like
            if date_after is None:
                date_after = _parse_optional_date(parsed_query.date_after)
            if date_before is None:
                date_before = _parse_optional_date(parsed_query.date_before)
            if has_attachments is None:
                has_attachments = parsed_query.has_attachments
            if attachment_type is None:
                attachment_type = parsed_query.attachment_type
            if attachment_name is None:
                attachment_name = parsed_query.attachment_name

            print("Applied local auto-filters from query parser.")

    final_result_count = config.search_results
    retrieval_candidate_count = (
        max(final_result_count, config.rerank_max_candidates)
        if rerank_enabled
        else final_result_count
    )

    # Initialize services with context managers for proper cleanup
    with Database() as database, VectorStore() as vector_store:
        embedding_service = EmbeddingService()
        query_builder = QueryBuilder(database)

        # Check if any emails are indexed
        stats = vector_store.get_stats()
        db_stats = database.get_stats()
        if stats["total_emails"] == 0 and db_stats["total_emails"] == 0:
            print("No emails indexed yet. Please run 'index' command first.")
            return

        print(f"Searching in {stats['total_emails']} indexed emails...")

        # Check if we have filters
        has_filters = any(
            [
                from_addr,
                to_addr,
                subject,
                subject_like,
                date_after,
                date_before,
                has_attachments is not None,
                attachment_type,
                attachment_name,
            ]
        )

        if has_filters:
            # Hybrid search: filter first, then vector search on results
            print("Applying filters...")
            # Limit filtered results to prevent memory issues with large result sets
            filtered_emails = query_builder.build_query(
                from_addr=from_addr,
                to_addr=to_addr,
                subject=subject,
                subject_like=subject_like,
                date_after=date_after,
                date_before=date_before,
                has_attachments=has_attachments,
                attachment_type=attachment_type,
                attachment_name=attachment_name,
                limit=config.max_filtered_search_limit,
            )

            if not filtered_emails:
                print("No emails match the filters.")
                return

            print(f"Found {len(filtered_emails)} emails matching filters, searching semantically...")

            # Get file hashes for filtered emails
            file_hashes = [
                get_file_hash(email["file_path"])
                for email in filtered_emails
            ]
            filtered_hashes_set = set(file_hashes)

            # Generate query embedding
            query_embedding = embedding_service.embed_query(effective_query)

            # Issue #7: Optimize vector search limit based on filtered result count
            # If we have fewer filtered emails than desired results, search exactly that many
            # Otherwise, search proportionally more but cap at a reasonable limit
            desired_candidates = retrieval_candidate_count
            if len(filtered_emails) <= desired_candidates:
                # Small result set - search all filtered emails
                vector_limit = len(filtered_emails)
            else:
                # Larger result set - search with some overhead for ranking
                vector_limit = min(desired_candidates * 2, len(filtered_emails), 1000)

            vector_results = vector_store.search(
                query_embedding, n_results=vector_limit
            )

            # Filter vector results to only include filtered emails
            filtered_vector_results = [
                r for r in vector_results
                if get_file_hash(r.get("file_path", "")) in filtered_hashes_set
            ]

            # If we didn't get enough results, expand the search
            if len(filtered_vector_results) < desired_candidates and vector_limit < len(filtered_emails):
                # Search with larger limit
                vector_results = vector_store.search(
                    query_embedding, n_results=min(len(filtered_emails), 1000)
                )
                filtered_vector_results = [
                    r for r in vector_results
                    if get_file_hash(r.get("file_path", "")) in filtered_hashes_set
                ]

            # Limit to candidate budget before reranking/final cut
            filtered_vector_results = filtered_vector_results[:desired_candidates]

            # Enrich with database metadata
            results = []
            for vr in filtered_vector_results:
                file_hash = get_file_hash(vr.get("file_path", ""))
                db_email = database.get_email_by_file_hash(file_hash)
                if db_email:
                    # Merge vector search result with database metadata
                    result = {**vr, **db_email}
                    result["similarity"] = 1 - vr.get("distance", 0)
                    results.append(result)
                else:
                    results.append(vr)

        else:
            # Pure vector search
            query_embedding = embedding_service.embed_query(effective_query)
            vector_results = vector_store.search(
                query_embedding, n_results=retrieval_candidate_count
            )

            # Enrich with database metadata
            results = []
            for vr in vector_results:
                file_hash = get_file_hash(vr.get("file_path", ""))
                db_email = database.get_email_by_file_hash(file_hash)
                if db_email:
                    result = {**vr, **db_email}
                    result["similarity"] = 1 - vr.get("distance", 0)
                    results.append(result)
                else:
                    results.append(vr)

        if rerank_enabled and results:
            print(
                f"Reranking top {min(len(results), retrieval_candidate_count)} candidates with local cross-encoder..."
            )
            reranker = CrossEncoderReranker()
            results = reranker.rerank(
                effective_query, results, top_k=final_result_count
            )
        else:
            results = results[:final_result_count]

        # Display results
        display_results(results, show_attachments=show_attachments)


