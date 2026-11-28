"""Retrieve attachments from indexed emails or stage them for local clients.

The primary index lives wherever EMAIL_DIR points — often an external volume
that an MCP client's filesystem sandbox cannot reach. This module stages the
selected email under a path inside the user's home (default
~/Documents/mailmate-staged/<hash>/) so a tool-using LLM can `Read` the
attachment bytes directly.

Idempotent: re-staging the same email returns the same directory. Existing
files are overwritten on each call (so an updated .eml on disk propagates).
"""

from __future__ import annotations

import base64
import email
import email.header
import email.policy
import hashlib
import os
import re
import shutil
import stat
from collections import defaultdict, deque
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO, Dict, Iterator, List, NamedTuple, Optional

from mail_semantic_search.config import config
from mail_semantic_search.database import Database, get_file_hash

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")


def _safe_filename(name: str, fallback: str) -> str:
    """Strip unsafe filename chars; collapse whitespace; bound length."""
    name = (name or "").strip()
    if not name:
        return fallback
    name = _SAFE_FILENAME_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return fallback
    return name[:200]  # filesystem limit guard


def _decode_filename(raw_filename: str) -> str:
    """Decode encoded attachment filenames returned by the email parser."""
    if not raw_filename:
        return ""
    try:
        decoded = email.header.decode_header(raw_filename)
    except email.errors.HeaderParseError:
        return raw_filename
    parts: List[str] = []
    for piece, enc in decoded:
        if isinstance(piece, bytes):
            try:
                parts.append(piece.decode(enc or "utf-8", errors="ignore"))
            except (LookupError, ValueError):
                parts.append(piece.decode("utf-8", errors="ignore"))
        else:
            parts.append(piece)
    return "".join(parts)


def _resolve_email_row(
    database: Database,
    file_path: Optional[str],
    message_id: Optional[str],
    *,
    match_both: bool = False,
) -> Optional[Dict]:
    """Look up the email row by file_path or message_id."""
    candidates = []
    if message_id:
        bare_id = message_id.removeprefix("<").removesuffix(">")
        candidates = [message_id, bare_id if bare_id != message_id else f"<{bare_id}>"]
    if file_path:
        cur = database.conn.cursor()
        cur.execute(
            "SELECT id, file_path, subject, message_id FROM emails "
            "WHERE file_path = ? LIMIT 1",
            (file_path,),
        )
        row = cur.fetchone()
        if row and match_both and message_id and row["message_id"] not in candidates:
            return None
        return dict(row) if row else None
    if message_id:
        cur = database.conn.cursor()
        for mid in candidates:
            cur.execute(
                "SELECT id, file_path, subject, message_id FROM emails "
                "WHERE message_id = ? LIMIT 1",
                (mid,),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
    return None


class AttachmentPart(NamedTuple):
    filename: str
    safe_name: str
    content_type: str
    payload: Optional[bytes]
    indexed_size: int


def iter_attachment_parts(msg: email.message.Message) -> Iterator[AttachmentPart]:
    """Yield attachment metadata and decoded payloads in MIME traversal order."""
    used_names: set[str] = set()
    for idx, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in disposition and "inline" not in disposition:
            continue
        filename = _decode_filename(part.get_filename() or "")
        content_type = part.get_content_type() or "application/octet-stream"
        subtype = content_type.partition("/")[2]
        extension = _safe_filename(f".{subtype}", "") if subtype != "octet-stream" else ""
        fallback = f"attachment-{idx}{extension}"[:200]
        name = _safe_filename(filename, fallback)
        if name in {".", ".."}:
            name = fallback
        stem, suffix = os.path.splitext(name)
        safe_name = name
        number = 1
        while safe_name in used_names:
            number += 1
            marker = f"-{number}"
            # Keep the extension when possible and reserve space for the marker.
            short_suffix = suffix[: 200 - len(marker)]
            safe_name = f"{stem[: 200 - len(marker) - len(short_suffix)]}{marker}{short_suffix}"
        used_names.add(safe_name)

        try:
            payload = part.get_payload(decode=True)
        except (LookupError, TypeError, ValueError):
            payload = None
        try:
            indexed_size = int(part.get("Content-Length") or 0)
        except (TypeError, ValueError):
            indexed_size = 0
        # Indexing uses Content-Length when available. Fetch limits and
        # response metadata always use the actual decoded byte count.
        indexed_size = indexed_size or len(payload or b"")
        # The email parser returns the encoded value unchanged when base64
        # cannot be decoded at all. Keep the index key, but don't return it.
        if any(isinstance(defect, email.errors.InvalidBase64LengthDefect) for defect in part.defects):
            payload = None
        yield AttachmentPart(filename, safe_name, content_type, payload, indexed_size)


def _open_email_source(path: Path) -> BinaryIO:
    """Open a resolved path without following links that appear after validation."""
    directories = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, directories)
    try:
        for name in path.parts[1:-1]:
            child_fd = os.open(name, directories, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        file_fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise OSError("Source must be a regular file")
            return os.fdopen(file_fd, "rb")
        except BaseException:
            os.close(file_fd)
            raise
    finally:
        os.close(fd)


def _fetch_limit(requested: Optional[int], configured: int) -> int:
    if requested is None:
        return configured
    if requested < 0:
        raise ValueError("Attachment fetch limits must be nonnegative byte counts")
    return min(requested, configured)


def _skip_reason(size: int, per_file: int, remaining: int) -> Optional[str]:
    if size > per_file:
        return "too_large"
    if size > remaining:
        return "over_total"
    return None


def _fetch_entry(
    filename: str,
    safe_name: str,
    content_type: str,
    size: Optional[int],
    payload: Optional[bytes],
    skipped: Optional[str],
) -> Dict:
    return {
        "filename": filename,
        "safe_name": safe_name,
        "content_type": content_type,
        "size": size,
        "sha256": hashlib.sha256(payload).hexdigest() if payload is not None else None,
        "data_base64": base64.b64encode(payload).decode("ascii") if payload is not None else None,
        "skipped": skipped,
    }


def fetch_attachments(
    message_id: Optional[str] = None,
    file_path: Optional[str] = None,
    attachment_ids: Optional[list[int]] = None,
    filenames: Optional[list[str]] = None,
    include_eml: bool = False,
    max_bytes_per_file: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
) -> Dict:
    """Read an indexed .eml and return bounded attachment bytes inline."""
    if not file_path and not message_id:
        raise ValueError("Pass message_id or file_path")
    per_file = _fetch_limit(max_bytes_per_file, config.max_attachment_fetch_bytes)
    remaining = _fetch_limit(max_total_bytes, config.max_attachment_fetch_total)
    requested_ids = None if attachment_ids is None else set(attachment_ids)
    requested_names = None if filenames is None else set(filenames)
    result = {
        "status": "not_indexed",
        "message_id": message_id,
        "file_path": file_path,
        "attachments": [],
        "eml": None,
        "unmatched_attachment_ids": sorted(requested_ids or ()),
    }
    if not config.database_path.is_file():
        return result
    with Database(read_only=True) as database:
        # Take the email row and its attachment IDs from the same snapshot.
        database.conn.execute("BEGIN")
        row = _resolve_email_row(database, file_path, message_id, match_both=True)
        if row is None:
            return result
        indexed_attachments = database.get_attachments(row["id"])

    result.update(message_id=row["message_id"], file_path=row["file_path"])
    source_path = Path(row["file_path"])
    with ExitStack() as stack:
        try:
            root = config.email_dir.resolve()
            resolved = source_path.resolve()
            if (
                not resolved.is_relative_to(root)
                or source_path.suffix.lower() != ".eml"
                or resolved.suffix.lower() != ".eml"
            ):
                result["status"] = "source_outside_email_dir"
                return result
        except (OSError, RuntimeError, ValueError):
            result["status"] = "source_unreadable"
            return result

        try:
            source = stack.enter_context(_open_email_source(resolved))
            msg = email.message_from_binary_file(source, policy=email.policy.default)
        except FileNotFoundError:
            result["status"] = "source_missing"
            return result
        except (OSError, ValueError, email.errors.MessageError):
            result["status"] = "source_unreadable"
            return result

        ids_by_part = defaultdict(deque)
        for attachment in indexed_attachments:
            key = (attachment["filename"], attachment["content_type"], attachment["size"])
            ids_by_part[key].append(attachment["id"])

        unmatched_ids = set(requested_ids or ())
        for part in iter_attachment_parts(msg):
            key = (part.filename, part.content_type, part.indexed_size)
            matching_ids = ids_by_part.get(key)
            attachment_id = matching_ids.popleft() if matching_ids else None
            unmatched_ids.discard(attachment_id)
            if requested_ids is not None and attachment_id not in requested_ids:
                continue
            filename = part.filename or part.safe_name
            if requested_names is not None and filename not in requested_names:
                continue

            payload = part.payload
            size = len(payload) if payload is not None else None
            skipped = "unreadable" if size is None else _skip_reason(size, per_file, remaining)
            entry = _fetch_entry(
                filename, part.safe_name, part.content_type, size,
                payload if skipped is None else None, skipped,
            )
            entry["id"] = attachment_id
            result["attachments"].append(entry)
            if skipped is None and size is not None:
                remaining -= size

        if include_eml:
            result["eml"] = _fetch_source_eml(source, per_file, remaining)
        result.update(status="ok", unmatched_attachment_ids=sorted(unmatched_ids))
        return result


def _fetch_source_eml(source: BinaryIO, per_file: int, remaining: int) -> Dict:
    """Return the raw message if it fits the remaining response budget."""
    payload = None
    size = None
    try:
        size = os.fstat(source.fileno()).st_size
        skipped = _skip_reason(size, per_file, remaining)
        if skipped is None:
            source.seek(0)
            payload = source.read(size + 1)
            # Recheck if an index source was modified while the call was in flight.
            size = max(len(payload), os.fstat(source.fileno()).st_size)
            skipped = _skip_reason(size, per_file, remaining)
            if size != len(payload) and skipped is None:
                skipped = "unreadable"
    except OSError:
        skipped = "unreadable"
    return _fetch_entry(
        "message.eml", "message.eml", "message/rfc822", size,
        payload if skipped is None else None, skipped,
    )


def stage_email(
    file_path: Optional[str] = None,
    message_id: Optional[str] = None,
    include_eml: bool = True,
) -> Dict:
    """Copy an indexed email's attachments (and .eml) to a sandbox-accessible dir.

    Args:
        file_path: Exact indexed file_path. One of file_path/message_id required.
        message_id: RFC-822 Message-ID, with or without angle brackets.
        include_eml: Also copy the source .eml into the staged dir.

    Returns:
        {
          "status": "ok" | "not_indexed" | "source_missing",
          "file_path": str,         # the resolved source .eml path
          "staged_dir": str,        # absolute path of the per-email directory
          "eml_path": str | None,   # absolute path of the staged .eml (if include_eml)
          "attachments": [          # one entry per saved attachment
            {"filename": str, "path": str, "size": int, "content_type": str}
          ],
          "message": str,           # human-readable detail
        }
    """
    if not file_path and not message_id:
        raise ValueError("Pass file_path or message_id")

    with Database() as database:
        row = _resolve_email_row(database, file_path, message_id)

    if row is None:
        return {
            "status": "not_indexed",
            "file_path": file_path or "",
            "staged_dir": "",
            "eml_path": None,
            "attachments": [],
            "message": "No indexed email matched the selector.",
        }

    source = Path(row["file_path"])
    if not source.exists():
        return {
            "status": "source_missing",
            "file_path": str(source),
            "staged_dir": "",
            "eml_path": None,
            "attachments": [],
            "message": f"Source .eml does not exist on disk: {source}",
        }

    # Stage under a per-email subdir keyed by the same hash used for Chroma
    # IDs — short, stable, collision-free across renames.
    short_hash = get_file_hash(str(source))[:12]
    staged_dir = (config.staging_dir / short_hash).resolve()
    staged_dir.mkdir(parents=True, exist_ok=True)
    attachments_dir = staged_dir / "attachments"
    attachments_dir.mkdir(exist_ok=True)

    eml_path: Optional[Path] = None
    if include_eml:
        eml_path = staged_dir / "message.eml"
        shutil.copyfile(source, eml_path)

    # Walk the email to write each attachment as its own decoded file.
    with open(source, "rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)

    saved: List[Dict] = []
    for part in iter_attachment_parts(msg):
        if not part.payload:
            continue

        out_path = attachments_dir / part.safe_name
        out_path.write_bytes(part.payload)
        saved.append(
            {
                "filename": part.filename or part.safe_name,
                "path": str(out_path),
                "size": out_path.stat().st_size,
                "content_type": part.content_type,
            }
        )

    return {
        "status": "ok",
        "file_path": str(source),
        "staged_dir": str(staged_dir),
        "eml_path": str(eml_path) if eml_path else None,
        "attachments": saved,
        "message": (
            f"Staged {len(saved)} attachment(s)"
            + (" + .eml" if eml_path else "")
            + f" to {staged_dir}"
        ),
    }


def clear_staged(short_hash: Optional[str] = None) -> Dict:
    """Remove staged files.

    With short_hash, removes only that per-email dir. Without, removes every
    staged dir under the staging root (but leaves the staging root itself).
    """
    root = config.staging_dir
    if not root.exists():
        return {"status": "ok", "removed": 0, "message": f"Staging dir does not exist: {root}"}

    if short_hash:
        target = (root / short_hash).resolve()
        # Defensive: ensure the resolved target is still under the staging
        # root, otherwise reject (paranoia about caller-supplied input).
        try:
            target.relative_to(root.resolve())
        except ValueError:
            raise ValueError(f"short_hash {short_hash!r} resolved outside staging root")
        if not target.exists():
            return {"status": "ok", "removed": 0, "message": f"No staged dir for {short_hash}"}
        shutil.rmtree(target)
        return {"status": "ok", "removed": 1, "message": f"Removed {target}"}

    removed = 0
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
            removed += 1
    return {"status": "ok", "removed": removed, "message": f"Removed {removed} staged email(s) from {root}"}
