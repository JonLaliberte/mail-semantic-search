"""Copy an indexed email's attachments (and .eml) to a path the LLM can read.

The primary index lives wherever EMAIL_DIR points — often an external volume
that an MCP client's filesystem sandbox cannot reach. This module stages the
selected email under a path inside the user's home (default
~/Documents/mailmate-staged/<hash>/) so a tool-using LLM can `Read` the
attachment bytes directly.

Idempotent: re-staging the same email returns the same directory. Existing
files are overwritten on each call (so an updated .eml on disk propagates).
"""

from __future__ import annotations

import email
import email.header
import email.policy
import logging
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from mail_semantic_search.config import config
from mail_semantic_search.database import Database, get_file_hash

logger = logging.getLogger(__name__)

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
    """Decode RFC2047 / RFC2231 encoded attachment filenames."""
    if not raw_filename:
        return ""
    decoded = email.header.decode_header(raw_filename)
    parts: List[str] = []
    for piece, enc in decoded:
        if isinstance(piece, bytes):
            try:
                parts.append(piece.decode(enc or "utf-8", errors="ignore"))
            except (LookupError, UnicodeDecodeError):
                parts.append(piece.decode("utf-8", errors="ignore"))
        else:
            parts.append(piece)
    return "".join(parts)


def _resolve_email_row(
    database: Database,
    file_path: Optional[str],
    message_id: Optional[str],
) -> Optional[Dict]:
    """Look up the email row by file_path or message_id."""
    if file_path:
        cur = database.conn.cursor()
        cur.execute(
            "SELECT id, file_path, subject, message_id FROM emails "
            "WHERE file_path = ? LIMIT 1",
            (file_path,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    if message_id:
        # Tolerate caller passing the bare value with or without <>.
        candidates = [message_id]
        if not (message_id.startswith("<") and message_id.endswith(">")):
            candidates.append(f"<{message_id}>")
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
    seen_names: Dict[str, int] = {}
    for idx, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in disposition and "inline" not in disposition:
            continue
        raw_filename = _decode_filename(part.get_filename() or "")
        content_type = part.get_content_type() or "application/octet-stream"
        ext = ""
        if "/" in content_type:
            ext_guess = content_type.split("/", 1)[1].split(";", 1)[0].strip()
            if ext_guess and ext_guess != "octet-stream":
                ext = "." + ext_guess
        safe = _safe_filename(raw_filename, fallback=f"attachment-{idx}{ext}")
        # Deduplicate filenames within a single email.
        if safe in seen_names:
            seen_names[safe] += 1
            stem = Path(safe).stem
            suffix = Path(safe).suffix
            safe = f"{stem}-{seen_names[safe]}{suffix}"
        else:
            seen_names[safe] = 1

        try:
            payload = part.get_payload(decode=True)
        except Exception as e:
            logger.warning("staging: could not decode attachment %s: %s", raw_filename, e)
            continue
        if not payload:
            continue

        out_path = attachments_dir / safe
        out_path.write_bytes(payload)
        saved.append(
            {
                "filename": raw_filename or safe,
                "path": str(out_path),
                "size": out_path.stat().st_size,
                "content_type": content_type,
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
