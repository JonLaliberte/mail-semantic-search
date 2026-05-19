"""Read and parse MailMate .eml files."""

import email
import email.policy
import logging
import multiprocessing as mp
import re
import subprocess
import threading
from email.utils import parsedate_to_datetime
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from tqdm import tqdm
try:
    # Older unquotemail releases
    from unquotemail import UnquoteMail  # type: ignore[attr-defined]
except ImportError:
    UnquoteMail = None  # type: ignore[assignment]

try:
    # Newer unquotemail releases
    from unquotemail import Unquote  # type: ignore[attr-defined]
except ImportError:
    Unquote = None  # type: ignore[assignment]

from mail_semantic_search.attachment_extractor import extract_text_from_attachment
from mail_semantic_search.config import config
from mail_semantic_search.database import validate_file_path

logger = logging.getLogger(__name__)

# Initialize parser once when legacy API is available
_unquote_parser = UnquoteMail() if UnquoteMail is not None else None
_reader_status_lock = threading.Lock()
_reader_status: Dict[str, Optional[str]] = {
    "current_file": None,
    "last_file": None,
}


def _update_reader_status(current_file: Optional[Path] = None, last_file: Optional[Path] = None) -> None:
    with _reader_status_lock:
        if current_file is not None:
            _reader_status["current_file"] = str(current_file)
        if last_file is not None:
            _reader_status["last_file"] = str(last_file)


def clear_reader_status() -> None:
    """Clear currently tracked parse file details."""
    with _reader_status_lock:
        _reader_status["current_file"] = None


def get_reader_status() -> Dict[str, Optional[str]]:
    """Return current reader diagnostics for indexing heartbeats."""
    with _reader_status_lock:
        return dict(_reader_status)


def _get_raw_header(msg, header_name: str) -> str:
    """Return the raw header value without triggering structured parsing."""
    try:
        for key, value in msg.raw_items():
            if key.lower() == header_name.lower():
                return value
    except AttributeError:
        pass
    return ""


def _get_safe_header(msg, header_name: str) -> str:
    """Read a header while tolerating malformed structured header values."""
    try:
        value = msg.get(header_name, "")
        return str(value) if value is not None else ""
    except (AttributeError, IndexError, TypeError, ValueError) as e:
        raw_value = _get_raw_header(msg, header_name)
        if raw_value:
            logger.debug(
                "Falling back to raw %s header for malformed message: %s",
                header_name,
                e,
            )
            return raw_value
        logger.debug("Failed to read %s header: %s", header_name, e)
        return ""


def _has_reply_markers(text: str) -> bool:
    lower = text.lower()
    return (
        ">" in text
        or "wrote:" in lower
        or "original message" in lower
        or "-----" in text
        or "\nfrom:" in lower
    )


def _should_strip_quotes(text: str) -> Tuple[bool, Optional[str]]:
    if not config.quote_strip_enabled:
        return False, "disabled"
    if len(text) > config.quote_strip_max_chars:
        return False, f"body_too_large_chars={len(text)}"
    line_count = text.count("\n") + 1
    if line_count > config.quote_strip_max_lines:
        return False, f"body_too_large_lines={line_count}"
    if not _has_reply_markers(text):
        return False, "no_reply_markers"
    return True, None


def _run_unquote(text: str, parser) -> str:
    if parser is not None:
        return parser.parse(text)
    if Unquote is not None:
        return Unquote(html=None, text=text).get_text()
    return text


def _quote_strip_worker(conn) -> None:
    parser = UnquoteMail() if UnquoteMail is not None else None
    while True:
        try:
            payload = conn.recv()
        except (EOFError, BrokenPipeError):
            break

        if payload is None:
            break

        try:
            stripped = _run_unquote(payload, parser)
            try:
                conn.send((True, stripped))
            except BrokenPipeError:
                break
        except (ValueError, TypeError, AttributeError, RuntimeError) as e:
            try:
                conn.send((False, str(e)))
            except BrokenPipeError:
                break
    try:
        conn.close()
    except OSError:
        pass


class QuoteStripper:
    """Run quoted-reply stripping in a dedicated worker process."""

    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = max(timeout_seconds, 0.1)
        self._ctx = mp.get_context("spawn")
        self._conn = None
        self._process = None

    def _ensure_worker(self) -> bool:
        if self._process is not None and self._process.is_alive() and self._conn is not None:
            return True

        parent_conn, child_conn = self._ctx.Pipe()
        process = self._ctx.Process(target=_quote_strip_worker, args=(child_conn,), daemon=True)
        process.start()
        child_conn.close()
        self._conn = parent_conn
        self._process = process
        return True

    def _restart_worker(self) -> None:
        self.close()
        self._ensure_worker()

    def strip(self, text: str, file_path: Optional[Path] = None) -> str:
        if UnquoteMail is None and Unquote is None:
            return text

        try:
            self._ensure_worker()
            self._conn.send(text)
            if not self._conn.poll(self.timeout_seconds):
                if file_path is not None:
                    logger.warning(
                        "Quoted-reply stripping timed out after %.1fs for %s; using original body.",
                        self.timeout_seconds,
                        file_path.name,
                    )
                self._restart_worker()
                return text

            ok, payload = self._conn.recv()
            if ok:
                return payload

            if file_path is not None:
                logger.debug(
                    f"Quoted-reply stripping failed for {file_path.name}: {payload}"
                )
            return text
        except (BrokenPipeError, EOFError, OSError) as e:
            if file_path is not None:
                logger.debug(f"Quote-strip worker failure for {file_path.name}: {e}")
            self._restart_worker()
            return text

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.send(None)
            except (BrokenPipeError, EOFError, OSError):
                pass
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

        if self._process is not None:
            self._process.join(timeout=0.2)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=1.0)
            self._process = None


_quote_stripper = QuoteStripper(config.quote_strip_timeout_seconds)


def extract_text_from_part(part) -> Optional[str]:
    """Extract text content from an email part."""
    if part.is_multipart():
        text_parts = []
        for subpart in part.walk():
            if subpart.get_content_type() == "text/plain":
                payload = subpart.get_payload(decode=True)
                if payload:
                    try:
                        charset = subpart.get_content_charset() or "utf-8"
                        text_parts.append(payload.decode(charset, errors="ignore"))
                    except (UnicodeDecodeError, LookupError):
                        text_parts.append(payload.decode("utf-8", errors="ignore"))
        return "\n".join(text_parts) if text_parts else None
    else:
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                try:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="ignore")
                except (UnicodeDecodeError, LookupError):
                    return payload.decode("utf-8", errors="ignore")
    return None


def remove_quoted_reply(text: str, file_path: Optional[Path] = None) -> str:
    """Remove quoted reply sections from email body."""
    if not text:
        return text

    should_strip, reason = _should_strip_quotes(text)
    if not should_strip:
        if file_path is not None and reason not in {"no_reply_markers"}:
            logger.debug(f"Skipping quoted-reply stripping for {file_path.name}: {reason}")
        return text

    if config.quote_strip_timeout_seconds > 0:
        return _quote_stripper.strip(text, file_path=file_path)

    try:
        return _run_unquote(text, _unquote_parser)
    except (ValueError, TypeError, AttributeError, RuntimeError) as e:
        # Issue #11: Log specific exceptions instead of swallowing silently
        logger.debug(f"Could not parse quoted reply: {e}")
        return text


def extract_attachments(msg, extract_text: bool = True) -> List[Dict]:
    """
    Extract attachment information from email message.
    
    Args:
        msg: Email message object
        extract_text: Whether to extract text content from attachments (default: True)
    
    Returns:
        List of attachment dictionaries with metadata and optionally extracted text
    """
    attachments = []
    
    for part in msg.walk():
        # Skip multipart containers
        if part.is_multipart():
            continue
            
        # Get content disposition
        content_disposition = part.get("Content-Disposition", "")
        if not content_disposition:
            continue
            
        # Check if it's an attachment
        if "attachment" in content_disposition.lower() or "inline" in content_disposition.lower():
            attachment = {}
            
            # Get filename
            filename = part.get_filename()
            if filename:
                # Decode filename if needed
                decoded_filename = email.header.decode_header(filename)
                if decoded_filename:
                    filename_parts = []
                    for part_data, encoding in decoded_filename:
                        if isinstance(part_data, bytes):
                            try:
                                filename_parts.append(part_data.decode(encoding or "utf-8", errors="ignore"))
                            except (UnicodeDecodeError, LookupError, ValueError):
                                filename_parts.append(part_data.decode("utf-8", errors="ignore"))
                        else:
                            filename_parts.append(str(part_data))
                    filename = "".join(filename_parts)
            
            attachment["filename"] = filename or ""
            attachment["content_type"] = part.get_content_type() or ""
            attachment["content_disposition"] = content_disposition
            
            # Get size (if available from Content-Length header)
            size = 0
            content_length = part.get("Content-Length")
            if content_length:
                try:
                    size = int(content_length)
                except (ValueError, TypeError):
                    pass
            
            # Issue #19: Skip very large attachments to prevent memory issues
            max_attachment_size = config.max_attachment_size
            if size > max_attachment_size:
                logger.debug(f"Skipping large attachment ({size} bytes): {attachment.get('filename', 'unknown')}")
                attachment["size"] = size
                attachments.append(attachment)
                continue
            
            # Get payload for size and text extraction
            payload = None
            if size == 0 or extract_text:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        if size == 0:
                            size = len(payload)
                        # Issue #19: Check size after decoding too
                        if size > max_attachment_size:
                            logger.debug(f"Skipping large decoded attachment ({size} bytes): {attachment.get('filename', 'unknown')}")
                            attachment["size"] = size
                            attachments.append(attachment)
                            continue
                except (UnicodeDecodeError, LookupError, ValueError) as e:
                    # Issue #11: Log specific exceptions
                    logger.debug(f"Could not decode attachment payload: {e}")
            
            attachment["size"] = size
            
            # Extract text content if requested
            if extract_text and payload:
                try:
                    extracted_text = extract_text_from_attachment(
                        payload, attachment["content_type"], attachment["filename"]
                    )
                    if extracted_text:
                        attachment["extracted_text"] = extracted_text
                except (UnicodeDecodeError, ValueError, OSError, TypeError, RuntimeError) as e:
                    # Issue #11: Log specific exceptions
                    logger.debug(f"Text extraction failed for {attachment.get('filename', 'unknown')}: {e}")
            
            attachments.append(attachment)
    
    return attachments


def parse_email_file(file_path: Path, base_dir: Optional[Path] = None) -> Optional[Dict]:
    """Parse a single .eml file and extract metadata and content.
    
    Args:
        file_path: Path to the .eml file
        base_dir: Optional base directory for path validation (Issue #3)
    """
    try:
        # Issue #3: Validate file path is within expected directory
        if base_dir and not validate_file_path(str(file_path), base_dir):
            logger.warning(f"Skipping file outside base directory: {file_path}")
            return None
        
        # Issue #5: Check file size before reading to prevent memory issues
        try:
            file_size = file_path.stat().st_size
            if file_size > config.max_email_file_size:
                logger.warning(f"Skipping large email file ({file_size} bytes): {file_path}")
                return None
        except OSError as e:
            logger.debug(f"Could not stat file {file_path}: {e}")
        
        # Parse from file object to avoid allocating an extra full-file bytes copy.
        with open(file_path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=email.policy.default)

        # Extract headers
        subject = _get_safe_header(msg, "Subject")
        if subject:
            # Decode subject if needed
            decoded_subject = email.header.decode_header(subject)
            subject = " ".join(
                [
                    part[0].decode(part[1] or "utf-8")
                    if isinstance(part[0], bytes)
                    else part[0]
                    for part in decoded_subject
                ]
            )

        from_addr = _get_safe_header(msg, "From")
        to_addrs = _get_safe_header(msg, "To")
        cc_addrs = _get_safe_header(msg, "Cc")
        bcc_addrs = _get_safe_header(msg, "Bcc")
        date_str = _get_safe_header(msg, "Date")
        message_id = _get_safe_header(msg, "Message-ID")

        # Parse date
        date_obj = None
        if date_str:
            try:
                date_obj = parsedate_to_datetime(date_str)
            except (ValueError, TypeError):
                pass

        # Extract body text
        body_text = extract_text_from_part(msg)

        # If no plain text, try to get HTML and strip tags (basic)
        if not body_text:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        try:
                            charset = part.get_content_charset() or "utf-8"
                            html_content = payload.decode(charset, errors="ignore")
                            # Basic HTML tag removal
                            body_text = re.sub(r"<[^>]+>", "", html_content)
                            break
                        except (UnicodeDecodeError, LookupError):
                            pass

        # Remove quoted replies from body text
        if body_text:
            body_text = remove_quoted_reply(body_text, file_path=file_path)

        # Extract attachments
        attachments = extract_attachments(msg)

        return {
            "subject": subject or "",
            "from": from_addr or "",
            "to": to_addrs or "",
            "cc": cc_addrs or "",
            "bcc": bcc_addrs or "",
            "date": date_obj,
            "message_id": message_id or "",
            "body": body_text or "",
            "file_path": str(file_path),
            "attachments": attachments,
        }
    except (OSError, ValueError, TypeError, email.errors.MessageError) as e:
        logger.warning(f"Failed to parse email file {file_path}: {e}")
        return None


def scan_eml_files(
    directory: Path, show_progress: bool = True, modified_after: Optional[datetime] = None
) -> Iterator[Path]:
    """Recursively scan directory for .eml files.
    
    Uses a generator to avoid loading all file paths into memory at once.
    """
    if modified_after is not None:
        # Fast path: use find to filter by modification timestamp.
        yield from _scan_eml_files_find(directory, modified_after)
        return

    if show_progress:
        # When showing progress, we still need to materialize for count
        eml_files = list(directory.rglob("*.eml"))
        for file_path in tqdm(eml_files, desc="Scanning .eml files"):
            yield file_path
    else:
        # Issue #4: Use generator directly when not showing progress
        for file_path in directory.rglob("*.eml"):
            yield file_path


def _scan_eml_files_find(directory: Path, modified_after: datetime) -> Iterator[Path]:
    """Use `find` to list .eml files newer than modified_after."""
    cutoff = modified_after.strftime("%Y-%m-%d %H:%M:%S")
    command = [
        "find",
        str(directory),
        "-type",
        "f",
        "-name",
        "*.eml",
        "-newermt",
        cutoff,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.debug(f"find command failed for cutoff scan: {result.stderr.strip()}")
            yield from _scan_eml_files_python(directory, modified_after)
            return

        for line in result.stdout.splitlines():
            if line:
                yield Path(line)
    except (OSError, ValueError) as e:
        logger.debug(f"Falling back from find-based scan: {e}")
        yield from _scan_eml_files_python(directory, modified_after)


def _scan_eml_files_python(directory: Path, modified_after: datetime) -> Iterator[Path]:
    """Fallback mtime-based scan when `find` is unavailable."""
    cutoff_ts = modified_after.timestamp()
    for file_path in directory.rglob("*.eml"):
        try:
            if file_path.stat().st_mtime > cutoff_ts:
                yield file_path
        except OSError:
            continue


def count_eml_files(directory: Path, modified_after: Optional[datetime] = None) -> int:
    """Count candidate .eml files, optionally filtered by modification date."""
    return sum(1 for _ in scan_eml_files(directory, show_progress=False, modified_after=modified_after))


def read_emails_batch(
    directory: Path,
    batch_size: int = 32,
    show_progress: bool = True,
    modified_after: Optional[datetime] = None,
    total_candidates: Optional[int] = None,
    max_emails: Optional[int] = None,
    should_skip: Optional[Callable[[Path], bool]] = None,
) -> Iterator[List[Dict]]:
    """Read emails in batches for efficient processing.

    Issue #4: No longer pre-counts files (which required scanning twice).
    Uses an unknown-total progress bar instead.

    Args:
        should_skip: Optional fast pre-filter invoked once per candidate path
            BEFORE parsing. Return True to skip the file (e.g. when path+mtime
            already match an indexed row). Avoids the cost of parsing .eml +
            attachment text for files we'd skip anyway.
    """
    batch = []
    processed_count = 0
    skipped_pre_parse = 0

    pbar = None
    if show_progress:
        pbar = tqdm(total=total_candidates, desc="Reading emails", unit=" emails")

    try:
        for file_path in scan_eml_files(directory, show_progress=False, modified_after=modified_after):
            if should_skip is not None and should_skip(file_path):
                skipped_pre_parse += 1
                if pbar is not None:
                    pbar.update(1)
                continue
            _update_reader_status(current_file=file_path)
            email_data = parse_email_file(file_path, base_dir=directory)
            _update_reader_status(last_file=file_path)
            if email_data:
                batch.append(email_data)
                if len(batch) >= batch_size:
                    batch_to_yield = batch
                    if max_emails is not None:
                        remaining = max_emails - processed_count
                        if remaining <= 0:
                            break
                        if len(batch) > remaining:
                            batch_to_yield = batch[:remaining]
                    processed_count += len(batch_to_yield)
                    if pbar is not None:
                        pbar.update(len(batch_to_yield))
                    yield batch_to_yield
                    if max_emails is not None and processed_count >= max_emails:
                        break
                    batch = []

        # Yield remaining emails
        if batch:
            batch_to_yield = batch
            if max_emails is not None:
                remaining = max_emails - processed_count
                if remaining > 0 and len(batch) > remaining:
                    batch_to_yield = batch[:remaining]
                elif remaining <= 0:
                    batch_to_yield = []
            processed_count += len(batch_to_yield)
            if pbar is not None:
                pbar.update(len(batch_to_yield))
            if batch_to_yield:
                yield batch_to_yield
    finally:
        clear_reader_status()
        _quote_stripper.close()
        if pbar is not None:
            pbar.close()
        if show_progress:
            logger.info(
                "Processed %d emails (skipped %d unchanged before parse)",
                processed_count,
                skipped_pre_parse,
            )


