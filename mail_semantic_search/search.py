"""Search logic for emails."""

from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from mail_semantic_search.config import config
from mail_semantic_search.database import Database, get_file_hash
from mail_semantic_search.embedding_service import EmbeddingService
from mail_semantic_search.query_parser import LocalQueryParser
from mail_semantic_search.query import QueryBuilder
from mail_semantic_search.reranker import CrossEncoderReranker
from mail_semantic_search.service_models import (
    QueryRequest,
    QueryResponse,
    SearchRequest,
    SearchResponse,
    StatusResponse,
)
from mail_semantic_search.vector_store import VectorStore


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


def _display_attachment_list(attachments: List[Dict]) -> None:
    """Display attachment details for a single email."""
    if not attachments:
        print("Attachments: none")
        return

    print(f"Attachments ({len(attachments)}):")
    for attachment in attachments:
        filename = attachment.get("filename", "Unknown")
        size = attachment.get("size", 0)
        content_type = attachment.get("content_type", "")
        extension = attachment.get("file_extension", "")
        details = []
        if size:
            details.append(f"{size:,} bytes")
        if content_type:
            details.append(content_type)
        if extension:
            details.append(f".{extension}")
        suffix = f" [{', '.join(details)}]" if details else ""
        print(f"  - {filename}{suffix}")


def get_indexed_email_data(file_path: str) -> Optional[Dict]:
    """Return the indexed SQLite and Chroma data for one email file path."""
    file_hash = get_file_hash(file_path)

    with Database() as database, VectorStore() as vector_store:
        db_email = database.get_email_by_file_hash(file_hash)
        if db_email and db_email.get("id") is not None:
            db_email["attachments"] = database.get_attachments(int(db_email["id"]))

        chroma_email = vector_store.get_email_document(file_path)

    if not db_email and not chroma_email:
        return None

    return {
        "file_path": file_path,
        "file_hash": file_hash,
        "sqlite": db_email,
        "chroma": chroma_email,
    }


def display_indexed_email(data: Dict) -> None:
    """Display a single indexed email from SQLite and Chroma."""
    print("Indexed Email")
    print("=" * 80)
    print(f"File Path: {data.get('file_path', 'Unknown')}")
    print(f"File Hash: {data.get('file_hash', 'Unknown')}")

    sqlite_email = data.get("sqlite")
    if sqlite_email:
        print("\nSQLite Metadata")
        print("-" * 80)
        print(f"DB ID: {sqlite_email.get('id', 'Unknown')}")
        print(f"Message-ID: {sqlite_email.get('message_id') or 'Unknown'}")
        print(f"Subject: {sqlite_email.get('subject') or 'No subject'}")
        print(f"From: {sqlite_email.get('from_addr') or 'Unknown'}")
        print(f"To: {sqlite_email.get('to_addrs') or ''}")
        print(f"Cc: {sqlite_email.get('cc_addrs') or ''}")
        print(f"Bcc: {sqlite_email.get('bcc_addrs') or ''}")
        print(f"Date: {format_date(str(sqlite_email.get('date', '')))}")
        print(f"Indexed At: {sqlite_email.get('indexed_at') or 'Unknown'}")
        print(f"File Mtime: {sqlite_email.get('file_mtime') or 'Unknown'}")
        print(f"Has Attachments: {bool(sqlite_email.get('has_attachments'))}")
        print(f"Attachment Count: {sqlite_email.get('attachment_count', 0)}")
        print(f"Body Preview: {sqlite_email.get('body_preview') or ''}")
        _display_attachment_list(sqlite_email.get("attachments", []))
    else:
        print("\nSQLite Metadata")
        print("-" * 80)
        print("No SQLite record found for this file path.")

    chroma_email = data.get("chroma")
    if chroma_email:
        print("\nChroma Document")
        print("-" * 80)
        print(f"Chroma ID: {chroma_email.get('id', 'Unknown')}")
        print("Metadata:")
        metadata = chroma_email.get("metadata", {}) or {}
        for key in sorted(metadata.keys()):
            print(f"  {key}: {metadata.get(key)}")
        print("\nIndexed Document:")
        print(chroma_email.get("document") or "")
    else:
        print("\nChroma Document")
        print("-" * 80)
        print("No Chroma record found for this file path.")


def _parse_optional_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse YYYY-MM-DD or ISO datetime, returning None on invalid."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        return None


def _filters_from_search_request(request: SearchRequest) -> Dict[str, object]:
    """Return a JSON-friendly filters dictionary."""
    return {
        "from_addr": request.from_addr,
        "to_addr": request.to_addr,
        "subject": request.subject,
        "subject_like": request.subject_like,
        "date_after": request.date_after.isoformat() if request.date_after else None,
        "date_before": request.date_before.isoformat() if request.date_before else None,
        "has_attachments": request.has_attachments,
        "attachment_type": request.attachment_type,
        "attachment_name": request.attachment_name,
    }


def _filters_from_query_request(request: QueryRequest) -> Dict[str, object]:
    """Return a JSON-friendly filters dictionary."""
    return {
        "from_addr": request.from_addr,
        "to_addr": request.to_addr,
        "subject": request.subject,
        "subject_like": request.subject_like,
        "date_after": request.date_after.isoformat() if request.date_after else None,
        "date_before": request.date_before.isoformat() if request.date_before else None,
        "has_attachments": request.has_attachments,
        "attachment_type": request.attachment_type,
        "attachment_name": request.attachment_name,
        "limit": request.limit,
    }


def _resolved_search_request(request: SearchRequest) -> Tuple[SearchRequest, bool]:
    """Apply parser-derived filters while preserving explicit overrides."""
    parser_enabled = (
        config.query_parser_enabled
        if request.auto_filters is None
        else request.auto_filters
    )
    if not parser_enabled:
        return request, False

    parsed_query = LocalQueryParser().parse(request.query)
    if not parsed_query:
        return request, False

    resolved = SearchRequest(
        query=request.query,
        from_addr=request.from_addr
        if request.from_addr is not None
        else parsed_query.from_addr,
        to_addr=request.to_addr if request.to_addr is not None else parsed_query.to_addr,
        subject=request.subject if request.subject is not None else parsed_query.subject,
        subject_like=request.subject_like
        if request.subject_like is not None
        else parsed_query.subject_like,
        date_after=request.date_after
        if request.date_after is not None
        else _parse_optional_date(parsed_query.date_after),
        date_before=request.date_before
        if request.date_before is not None
        else _parse_optional_date(parsed_query.date_before),
        has_attachments=request.has_attachments
        if request.has_attachments is not None
        else parsed_query.has_attachments,
        attachment_type=request.attachment_type
        if request.attachment_type is not None
        else parsed_query.attachment_type,
        attachment_name=request.attachment_name
        if request.attachment_name is not None
        else parsed_query.attachment_name,
        limit=request.limit,
        auto_filters=request.auto_filters,
        rerank=request.rerank,
    )

    if parsed_query.semantic_query:
        resolved.query = parsed_query.semantic_query

    return resolved, True


def _normalize_result(result: Dict) -> Dict:
    """Normalize a result to a stable JSON-friendly shape."""
    normalized = dict(result)
    if "from" in normalized and "from_addr" not in normalized:
        normalized["from_addr"] = normalized["from"]
    if "to" in normalized and "to_addrs" not in normalized:
        normalized["to_addrs"] = normalized["to"]
    normalized.setdefault("attachments", [])
    normalized.pop("from", None)
    normalized.pop("to", None)
    return normalized


def _dedup_results_by_message_id(results: List[Dict]) -> List[Dict]:
    """Collapse results with the same message_id, keeping the lowest distance.

    Rows with empty/None message_id are always preserved as distinct results
    because they cannot be correlated by content.
    """
    seen: Dict[str, int] = {}  # message_id -> index in output
    output: List[Dict] = []
    for result in results:
        mid = result.get("message_id") or ""
        if not mid:
            output.append(result)
            continue
        if mid not in seen:
            seen[mid] = len(output)
            output.append(result)
        else:
            existing_idx = seen[mid]
            existing_distance = output[existing_idx].get("distance") or 1.0
            this_distance = result.get("distance") or 1.0
            if this_distance < existing_distance:
                output[existing_idx] = result
    return output


def search_email_records(request: SearchRequest) -> SearchResponse:
    """Run semantic search and return structured results."""
    resolved_request, parser_applied = _resolved_search_request(request)
    effective_query = resolved_request.query
    final_result_count = resolved_request.limit or config.search_results

    rerank_enabled = (
        config.rerank_enabled if resolved_request.rerank is None else resolved_request.rerank
    )
    retrieval_candidate_count = (
        max(final_result_count, config.rerank_max_candidates)
        if rerank_enabled
        else final_result_count
    )

    with Database() as database, VectorStore() as vector_store:
        embedding_service = EmbeddingService()
        query_builder = QueryBuilder(database)

        vector_stats = vector_store.get_stats()
        db_stats = database.get_stats()
        if vector_stats["total_emails"] == 0 and db_stats["total_emails"] == 0:
            return SearchResponse(
                query=request.query,
                effective_query=effective_query,
                parser_applied=parser_applied,
                rerank_applied=False,
                indexed_emails=0,
                filters=_filters_from_search_request(resolved_request),
                message="No emails indexed yet. Please run 'index' command first.",
            )

        has_filters = any(
            [
                resolved_request.from_addr,
                resolved_request.to_addr,
                resolved_request.subject,
                resolved_request.subject_like,
                resolved_request.date_after,
                resolved_request.date_before,
                resolved_request.has_attachments is not None,
                resolved_request.attachment_type,
                resolved_request.attachment_name,
            ]
        )

        if has_filters:
            filtered_emails = query_builder.build_query(
                from_addr=resolved_request.from_addr,
                to_addr=resolved_request.to_addr,
                subject=resolved_request.subject,
                subject_like=resolved_request.subject_like,
                date_after=resolved_request.date_after,
                date_before=resolved_request.date_before,
                has_attachments=resolved_request.has_attachments,
                attachment_type=resolved_request.attachment_type,
                attachment_name=resolved_request.attachment_name,
                limit=config.max_filtered_search_limit,
            )

            if not filtered_emails:
                return SearchResponse(
                    query=request.query,
                    effective_query=effective_query,
                    parser_applied=parser_applied,
                    rerank_applied=False,
                    indexed_emails=vector_stats["total_emails"],
                    filters=_filters_from_search_request(resolved_request),
                    message="No emails match the filters.",
                )

            filtered_hashes_set = {
                get_file_hash(email["file_path"]) for email in filtered_emails
            }
            query_embedding = embedding_service.embed_query(effective_query)
            desired_candidates = retrieval_candidate_count

            if len(filtered_emails) <= desired_candidates:
                vector_limit = len(filtered_emails)
            else:
                vector_limit = min(desired_candidates * 2, len(filtered_emails), 1000)

            vector_results = vector_store.search(query_embedding, n_results=vector_limit)
            filtered_vector_results = [
                result
                for result in vector_results
                if get_file_hash(result.get("file_path", "")) in filtered_hashes_set
            ]

            if (
                len(filtered_vector_results) < desired_candidates
                and vector_limit < len(filtered_emails)
            ):
                expanded_results = vector_store.search(
                    query_embedding, n_results=min(len(filtered_emails), 1000)
                )
                filtered_vector_results = [
                    result
                    for result in expanded_results
                    if get_file_hash(result.get("file_path", "")) in filtered_hashes_set
                ]

            candidates = filtered_vector_results[:desired_candidates]
            results = []
            for candidate in candidates:
                file_hash = get_file_hash(candidate.get("file_path", ""))
                db_email = database.get_email_by_file_hash(file_hash)
                if db_email:
                    merged = {**candidate, **db_email}
                    merged["similarity"] = 1 - candidate.get("distance", 0)
                    results.append(_normalize_result(merged))
                else:
                    results.append(_normalize_result(candidate))
        else:
            query_embedding = embedding_service.embed_query(effective_query)
            vector_results = vector_store.search(
                query_embedding, n_results=retrieval_candidate_count
            )

            results = []
            for candidate in vector_results:
                file_hash = get_file_hash(candidate.get("file_path", ""))
                db_email = database.get_email_by_file_hash(file_hash)
                if db_email:
                    merged = {**candidate, **db_email}
                    merged["similarity"] = 1 - candidate.get("distance", 0)
                    results.append(_normalize_result(merged))
                else:
                    results.append(_normalize_result(candidate))

        results = _dedup_results_by_message_id(results)

        rerank_applied = False
        if rerank_enabled and results:
            results = CrossEncoderReranker().rerank(
                effective_query,
                results,
                top_k=final_result_count,
            )
            results = [_normalize_result(result) for result in results]
            rerank_applied = True
        else:
            results = results[:final_result_count]

        return SearchResponse(
            query=request.query,
            effective_query=effective_query,
            parser_applied=parser_applied,
            rerank_applied=rerank_applied,
            indexed_emails=vector_stats["total_emails"],
            filters=_filters_from_search_request(resolved_request),
            results=results,
        )


def query_email_records(request: QueryRequest) -> QueryResponse:
    """Run metadata-only query and return structured results."""
    with Database() as database:
        query_builder = QueryBuilder(database)
        results = query_builder.build_query(
            from_addr=request.from_addr,
            to_addr=request.to_addr,
            subject=request.subject,
            subject_like=request.subject_like,
            date_after=request.date_after,
            date_before=request.date_before,
            has_attachments=request.has_attachments,
            attachment_type=request.attachment_type,
            attachment_name=request.attachment_name,
            limit=request.limit,
        )
    return QueryResponse(
        filters=_filters_from_query_request(request),
        results=[_normalize_result(result) for result in results],
    )


def get_status_data() -> StatusResponse:
    """Return structured indexing status."""
    with Database() as database, VectorStore() as vector_store:
        vector_stats = vector_store.get_stats()
        db_stats = database.get_stats()
        return StatusResponse(
            embedding_model=config.embedding_model,
            email_directory=str(config.email_dir),
            chromadb_path=str(config.chromadb_path),
            database_path=str(config.database_path),
            total_indexed_emails=vector_stats["total_emails"],
            total_emails=db_stats["total_emails"],
            total_attachments=db_stats["total_attachments"],
            emails_with_attachments=db_stats["emails_with_attachments"],
            date_range=db_stats["date_range"],
            batch_size=config.batch_size,
            search_results=config.search_results,
        )


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
    response = search_email_records(
        SearchRequest(
            query=query,
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            subject_like=subject_like,
            date_after=date_after,
            date_before=date_before,
            has_attachments=has_attachments,
            attachment_type=attachment_type,
            attachment_name=attachment_name,
            auto_filters=auto_filters,
            rerank=rerank,
        )
    )

    print(f"Searching in {response.indexed_emails} indexed emails...")
    if response.parser_applied:
        print("Applied local auto-filters from query parser.")
    if response.filters and any(value is not None for value in response.filters.values()):
        print("Applying filters...")
    if response.rerank_applied and response.results:
        print(f"Reranking top {len(response.results)} candidates with local cross-encoder...")
    if response.message and not response.results:
        print(response.message)
        return
    display_results(response.results, show_attachments=show_attachments)


def search_email_records_payload(request: SearchRequest) -> Dict:
    """Return search response as a JSON-friendly dict."""
    return asdict(search_email_records(request))


def query_email_records_payload(request: QueryRequest) -> Dict:
    """Return query response as a JSON-friendly dict."""
    return asdict(query_email_records(request))


def get_status_data_payload() -> Dict:
    """Return status response as a JSON-friendly dict."""
    return asdict(get_status_data())


