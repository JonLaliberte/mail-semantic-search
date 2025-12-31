"""Read and parse MailMate .eml files."""

import email
import email.policy
import logging
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from tqdm import tqdm
from unquotemail import UnquoteMail

from mailmate_search.attachment_extractor import extract_text_from_attachment

logger = logging.getLogger(__name__)

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
                            except Exception:
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
            
            # Get payload for size and text extraction
            payload = None
            if size == 0 or extract_text:
                try:
                    payload = part.get_payload(decode=True)
                    if payload and size == 0:
                        size = len(payload)
                except Exception:
                    pass
            
            attachment["size"] = size
            
            # Extract text content if requested
            if extract_text and payload:
                try:
                    extracted_text = extract_text_from_attachment(
                        payload, attachment["content_type"], attachment["filename"]
                    )
                    if extracted_text:
                        attachment["extracted_text"] = extracted_text
                except Exception:
                    # If text extraction fails, continue without it
                    pass
            
            attachments.append(attachment)
    
    return attachments


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
        cc_addrs = msg.get("Cc", "")
        bcc_addrs = msg.get("Bcc", "")
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
    except Exception as e:
        logger.warning(f"Failed to parse email file {file_path}: {e}")
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

    pbar = None
    if show_progress:
        pbar = tqdm(total=total_files, desc="Reading emails")

    try:
        for file_path in scan_eml_files(directory, show_progress=False):
            email_data = parse_email_file(file_path)
            if email_data:
                batch.append(email_data)
                if len(batch) >= batch_size:
                    if pbar:
                        pbar.update(len(batch))
                    yield batch
                    batch = []

        # Yield remaining emails
        if batch:
            if pbar:
                pbar.update(len(batch))
            yield batch
    finally:
        if pbar:
            pbar.close()


