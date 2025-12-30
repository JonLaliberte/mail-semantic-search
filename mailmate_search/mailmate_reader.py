"""Read and parse MailMate .eml files."""

import email
import email.policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from tqdm import tqdm
from unquotemail import UnquoteMail

# Initialize unquote parser once for reuse
_unquote_parser = UnquoteMail()


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


def remove_quoted_reply(text: str) -> str:
    """Remove quoted reply sections from email body."""
    if not text:
        return text
    try:
        return _unquote_parser.parse(text)
    except Exception:
        # If parsing fails, return original text
        return text


def parse_email_file(file_path: Path) -> Optional[Dict]:
    """Parse a single .eml file and extract metadata and content."""
    try:
        with open(file_path, "rb") as f:
            msg = email.message_from_bytes(f.read(), policy=email.policy.default)

        # Extract headers
        subject = msg.get("Subject", "")
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

        from_addr = msg.get("From", "")
        to_addrs = msg.get("To", "")
        date_str = msg.get("Date", "")
        message_id = msg.get("Message-ID", "")

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
                            import re

                            body_text = re.sub(r"<[^>]+>", "", html_content)
                            break
                        except (UnicodeDecodeError, LookupError):
                            pass

        # Remove quoted replies from body text
        if body_text:
            body_text = remove_quoted_reply(body_text)

        return {
            "subject": subject or "",
            "from": from_addr or "",
            "to": to_addrs or "",
            "date": date_obj,
            "message_id": message_id or "",
            "body": body_text or "",
            "file_path": str(file_path),
        }
    except Exception as e:
        # Skip files that can't be parsed
        return None


def scan_eml_files(
    directory: Path, show_progress: bool = True
) -> Iterator[Path]:
    """Recursively scan directory for .eml files."""
    eml_files = list(directory.rglob("*.eml"))
    if show_progress:
        for file_path in tqdm(eml_files, desc="Scanning .eml files"):
            yield file_path
    else:
        for file_path in eml_files:
            yield file_path


def read_emails_batch(
    directory: Path, batch_size: int = 32, show_progress: bool = True
) -> Iterator[List[Dict]]:
    """Read emails in batches for efficient processing."""
    batch = []
    total_files = sum(1 for _ in directory.rglob("*.eml"))

    if show_progress:
        pbar = tqdm(total=total_files, desc="Reading emails")

    for file_path in scan_eml_files(directory, show_progress=False):
        email_data = parse_email_file(file_path)
        if email_data:
            batch.append(email_data)
            if len(batch) >= batch_size:
                if show_progress:
                    pbar.update(len(batch))
                yield batch
                batch = []

    # Yield remaining emails
    if batch:
        if show_progress:
            pbar.update(len(batch))
        yield batch

    if show_progress:
        pbar.close()


