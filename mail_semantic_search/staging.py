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
import binascii
import email
import email.header
import email.policy
import hashlib
import io
import os
import re
import shutil
import stat
from collections import defaultdict
from pathlib import Path
from typing import BinaryIO, Dict, Iterator, List, NamedTuple, Optional

from mail_semantic_search.config import config
from mail_semantic_search.database import Database, get_file_hash

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")
_WINDOWS_DEVICE_NAME = re.compile(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", re.IGNORECASE)
_BASE64_TOKENS = re.compile(r"[A-Za-z0-9+/]+|=+")
_QP_ESCAPE = re.compile(r"=[0-9A-Fa-f]{2}|==|=\r[^\n]*(?:\n|\Z)|=\n|=\Z")
_UU_LINE = re.compile(r"([^\r\n]*)(?:\r\n|\r|\n|\Z)")
_UU_MODE = re.compile(r"begin [+-]?(?:0[oO]_?)?[0-7](?:_?[0-7])*(?=[ \r\n]|\Z)")
_UU_END = re.compile(r"[ \t\f]*end[ \t\f]*")
_UU_BODY = re.compile(r"[ -`]*")
_UU_ENCODINGS = {"x-uuencode", "uuencode", "uue", "x-uue"}
_SOURCE_FILENAME = "message.eml"

# Match the email decoder used for indexing. Older Python releases compare
# the full transfer-encoding header, while newer releases use the parsed token.
_DECODER_USES_CTE_TOKEN = email.message_from_bytes(
    b"Content-Transfer-Encoding: base64 \r\n\r\nYWJj", policy=email.policy.default
).get_payload(decode=True) == b"abc"


def _safe_filename(name: str, fallback: str) -> str:
    """Strip unsafe filename chars; collapse whitespace; bound length."""
    name = (name or "").strip()
    if not name:
        return fallback
    name = _SAFE_FILENAME_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).lstrip(" .")[:200].rstrip(" .")
    if not name:
        return fallback
    if _WINDOWS_DEVICE_NAME.fullmatch(name.partition(".")[0].rstrip()):
        return ("_" + name)[:200].rstrip(" .")
    return name


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
    mime_part: email.message.Message


def _unique_name(name: str, used_names: set[str], next_suffix: dict[str, int]) -> str:
    """Allocate a unique basename, including on case-insensitive volumes."""
    stem, suffix = os.path.splitext(name)
    safe_name = name
    name_key = name.casefold()
    number = next_suffix.get(name_key, 2)
    while safe_name.casefold() in used_names:
        marker = f"-{number}"
        # Keep the extension when possible and reserve space for the marker.
        short_suffix = suffix[: 200 - len(marker)]
        candidate = f"{stem[: 200 - len(marker) - len(short_suffix)]}{marker}{short_suffix}"
        safe_name = _safe_filename(candidate, marker)
        number += 1
    next_suffix[name_key] = number
    used_names.add(safe_name.casefold())
    return safe_name


def iter_attachment_parts(msg: email.message.Message) -> Iterator[AttachmentPart]:
    """Yield named attachment leaves in MIME traversal order without decoding."""
    used_names: set[str] = set()
    next_suffix: dict[str, int] = {}
    for idx, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in disposition and "inline" not in disposition:
            continue
        filename = _decode_filename(part.get_filename() or "")
        content_type = part.get_content_type() or "application/octet-stream"
        subtype = content_type.partition("/")[2]
        extension = _safe_filename(subtype, "") if subtype != "octet-stream" else ""
        if extension:
            extension = "." + extension
        fallback = f"attachment-{idx}{extension}"[:200].rstrip(" .")
        name = _safe_filename(filename, fallback)
        yield AttachmentPart(filename, _unique_name(name, used_names, next_suffix), content_type, part)


def _decode_payload(
    part: email.message.Message, *, preserve_invalid_base64: bool = False
) -> Optional[bytes]:
    try:
        payload = part.get_payload(decode=True)
    except (LookupError, TypeError, ValueError, binascii.Error):
        return None
    if not preserve_invalid_base64 and any(
        isinstance(defect, email.errors.InvalidBase64LengthDefect) for defect in part.defects
    ):
        return None
    return payload


def _base64_size(raw: str) -> Optional[int]:
    """Count decoded bytes, including MIME line breaks and recoverable padding."""
    digits = 0
    padding = False
    for token in _BASE64_TOKENS.finditer(raw):
        if raw[token.start()] == "=":
            padding = True
        elif padding:
            # Python versions disagree on whether embedded padding ends
            # the stream. Treat it as malformed rather than guess its size.
            return None
        else:
            digits += token.end() - token.start()
    return None if digits % 4 == 1 else digits * 3 // 4


def _uu_size(raw: str) -> int:
    """Size legacy uuencoded data from each line's decoded-length character."""
    started = False
    size = 0
    for match in _UU_LINE.finditer(raw):
        if match.start() == match.end():
            break
        start, end = match.span(1)
        if not started:
            started = _UU_MODE.match(raw, start) is not None
            continue
        if start == end:
            return len(raw)  # The standard parser returns malformed uuencode as raw bytes.
        if _UU_END.fullmatch(raw, start, end):
            break
        line_size = (ord(raw[start]) - 32) & 63
        # The standard parser falls back to the line prefix on decoding errors.
        end = min(start + (line_size * 4 + 5) // 3, end)
        if not _UU_BODY.fullmatch(raw, start + 1, end):
            return len(raw)
        size += line_size
    return size if started else len(raw)


def _decoded_size(raw: str, encoding: str) -> Optional[int]:
    size = len(raw)
    if encoding == "base64":
        size = _base64_size(raw)
    elif encoding == "quoted-printable":
        for escape in _QP_ESCAPE.finditer(raw):
            length = escape.end() - escape.start()
            emits_byte = length > 1 and raw[escape.start() + 1] not in "\r\n"
            size -= length - emits_byte
    elif encoding in _UU_ENCODINGS:
        size = _uu_size(raw)
    return size


def _attachment_sizes(part: email.message.Message) -> tuple[Optional[int], tuple[Optional[int], ...]]:
    """Return the local decoded size and possible sizes recorded by the index."""
    # The parser stores escapes for non-ASCII bytes. get_payload(decode=False)
    # can replace them using the part's charset, which loses the byte count.
    raw = part._payload
    if not isinstance(raw, str):
        return None, (None,)
    cte = part.get("Content-Transfer-Encoding", "")
    old_encoding = str(cte).lower()
    token_encoding = getattr(cte, "cte", old_encoding.strip())
    local, other = (
        (token_encoding, old_encoding) if _DECODER_USES_CTE_TOKEN
        else (old_encoding, token_encoding)
    )
    size = _decoded_size(raw, local)
    try:
        indexed_size = int(part.get("Content-Length") or 0)
    except (TypeError, ValueError):
        indexed_size = 0
    if indexed_size:
        return size, (indexed_size,)

    # An unreadable transfer encoding has no trustworthy indexed size.
    # Different Python decoders can return raw bytes or a partial decode.
    if other == local:
        return size, (size,)
    return size, (size, _decoded_size(raw, other))


def _match_attachment_ids(
    part_sizes: list[tuple[Optional[int], ...]], rows: list[Dict]
) -> tuple[list[Optional[int]], dict[int, str]]:
    """Use MIME order for a complete match and mutually unique evidence for fallback."""
    if len(part_sizes) == len(rows) and any(
        all(sizes[variant] is not None and row["size"] == sizes[variant]
            for sizes, row in zip(part_sizes, rows))
        for variant in (0, -1)
    ):
        # Same-name leaves can share a possible size across runtimes. Pair them
        # in the order the indexer stored them before trying local sizes alone.
        return [row["id"] for row in rows], {}

    rows_by_size = defaultdict(int)
    for row in rows:
        rows_by_size[row["size"]] += 1

    positions_by_size = defaultdict(list)
    unknown_positions = []
    possible_rows_per_part = []
    for position, sizes in enumerate(part_sizes):
        if None in sizes:
            # This part could occupy any of the indexed rows in the group.
            # It blocks an apparent match but cannot establish one.
            unknown_positions.append(position)
            possible_rows_per_part.append(len(rows))
            continue
        distinct_sizes = set(sizes)
        for size in distinct_sizes:
            positions_by_size[size].append(position)
        possible_rows_per_part.append(sum(rows_by_size[size] for size in distinct_sizes))

    matched: list[Optional[int]] = [None] * len(part_sizes)
    reasons = {}
    for row in rows:
        candidates = positions_by_size.get(row["size"], [])
        count = len(candidates) + len(unknown_positions)
        if count == 0:
            continue
        if count > 1:
            reasons[row["id"]] = "ambiguous"
            continue
        position = candidates[0] if candidates else unknown_positions[0]
        if possible_rows_per_part[position] > 1:
            reasons[row["id"]] = "ambiguous"
        elif candidates:
            matched[position] = row["id"]
        else:
            reasons[row["id"]] = "size_unknown"
    return matched, reasons


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
        "unmatched_attachment_id_reasons": {},
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
        with _open_email_source(resolved) as source:
            source_limit = config.max_email_file_size
            source_size = os.fstat(source.fileno()).st_size
            if source_size <= source_limit:
                # fstat checks the opened file. The bounded read also catches
                # growth between checking the size and reading the contents.
                raw = source.read(source_limit + 1)
                source_size = len(raw)
            if source_size > source_limit:
                result["status"] = "source_too_large"
                if include_eml:
                    result["eml"] = _source_entry(source_size, None, "too_large")
                return result

        # Use the indexer's file parser, including universal newline handling.
        # Keep raw separately so include_eml can return the original bytes.
        with io.BytesIO(raw) as source:
            msg = email.message_from_binary_file(source, policy=email.policy.default)
    except FileNotFoundError:
        result["status"] = "source_missing"
        return result
    except (OSError, ValueError, RecursionError, email.errors.MessageError):
        result["status"] = "source_unreadable"
        return result

    source_payload = (
        raw if include_eml and _skip_reason(source_size, per_file, remaining) is None else None
    )
    del raw
    rows_by_part = defaultdict(list)
    selected_keys = set()
    for attachment in indexed_attachments:
        name_and_type = (attachment["filename"], attachment["content_type"])
        rows_by_part[name_and_type].append(attachment)
        if requested_ids is not None and attachment["id"] in requested_ids:
            selected_keys.add(name_and_type)

    parts = []
    positions_by_part = defaultdict(list)
    for part in iter_attachment_parts(msg):
        name_and_type = (part.filename, part.content_type)
        if requested_ids is not None and name_and_type not in selected_keys:
            continue
        filename = part.filename or part.safe_name
        if (
            requested_ids is None and requested_names is not None
            and part.filename and filename not in requested_names
        ):
            continue

        size, indexed_sizes = _attachment_sizes(part.mime_part)
        positions_by_part[name_and_type].append(len(parts))
        parts.append((part, filename, size, indexed_sizes))

    ids = [None] * len(parts)
    id_reasons = {}
    for key, positions in positions_by_part.items():
        matched, reasons = _match_attachment_ids(
            [parts[position][3] for position in positions], rows_by_part.get(key, [])
        )
        id_reasons.update(reasons)
        for position, attachment_id in zip(positions, matched, strict=True):
            ids[position] = attachment_id

    unmatched_ids = set(requested_ids or ())
    for (part, filename, size, _), attachment_id in zip(parts, ids, strict=True):
        unmatched_ids.discard(attachment_id)
        if requested_ids is not None and attachment_id not in requested_ids:
            continue
        if requested_names is not None and filename not in requested_names:
            continue
        if size == 0:
            continue  # Local staging also omits empty attachments.

        payload = None
        skipped = "unreadable" if size is None else _skip_reason(size, per_file, remaining)
        if skipped is None:
            payload = _decode_payload(part.mime_part)
            if not payload or len(payload) != size:
                # If a decoder disagrees with preflight, never return bytes
                # that were not included in the size check.
                payload = None
                skipped = "unreadable"
            else:
                remaining -= len(payload)
        entry = _fetch_entry(filename, part.safe_name, part.content_type, size, payload, skipped)
        entry["id"] = attachment_id
        result["attachments"].append(entry)

    if include_eml:
        skipped = _skip_reason(source_size, per_file, remaining)
        used_names = {entry["safe_name"].casefold() for entry in result["attachments"]}
        source_name = _unique_name(_SOURCE_FILENAME, used_names, {})
        result["eml"] = _source_entry(
            source_size, source_payload if skipped is None else None, skipped, source_name
        )
    result.update(
        status="ok",
        unmatched_attachment_ids=sorted(unmatched_ids),
        unmatched_attachment_id_reasons={
            str(attachment_id): id_reasons[attachment_id]
            for attachment_id in sorted(unmatched_ids) if attachment_id in id_reasons
        },
    )
    return result


def _source_entry(
    size: int, payload: Optional[bytes], skipped: Optional[str], safe_name: str = _SOURCE_FILENAME
) -> Dict:
    return _fetch_entry(_SOURCE_FILENAME, safe_name, "message/rfc822", size, payload, skipped)


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
        eml_path = staged_dir / _SOURCE_FILENAME
        shutil.copyfile(source, eml_path)

    # Walk the email to write each attachment as its own decoded file.
    with open(source, "rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)

    saved: List[Dict] = []
    for part in iter_attachment_parts(msg):
        # Preserve the raw fallback the local staging tool has always written.
        payload = _decode_payload(part.mime_part, preserve_invalid_base64=True)
        if not payload:
            continue

        out_path = attachments_dir / part.safe_name
        out_path.write_bytes(payload)
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
