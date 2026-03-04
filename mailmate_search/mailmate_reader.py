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
from mailmate_search.config import config
from mailmate_search.database import validate_file_path

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
    except (ValueError, TypeError, AttributeError) as e:
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
                except (UnicodeDecodeError, ValueError, OSError) as e:
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
    """Recursively scan directory for .eml files.
    
    Uses a generator to avoid loading all file paths into memory at once.
    """
    if show_progress:
        # When showing progress, we still need to materialize for count
        eml_files = list(directory.rglob("*.eml"))
        for file_path in tqdm(eml_files, desc="Scanning .eml files"):
            yield file_path
    else:
        # Issue #4: Use generator directly when not showing progress
        for file_path in directory.rglob("*.eml"):
            yield file_path


def read_emails_batch(
    directory: Path, batch_size: int = 32, show_progress: bool = True
) -> Iterator[List[Dict]]:
    """Read emails in batches for efficient processing.
    
    Issue #4: No longer pre-counts files (which required scanning twice).
    Uses an unknown-total progress bar instead.
    """
    batch = []
    processed_count = 0

    pbar = None
    if show_progress:
        # Issue #4: Don't pre-count files - use unknown total progress bar
        pbar = tqdm(desc="Reading emails", unit=" emails")

    try:
        for file_path in scan_eml_files(directory, show_progress=False):
            email_data = parse_email_file(file_path)
            if email_data:
                batch.append(email_data)
                if len(batch) >= batch_size:
                    processed_count += len(batch)
                    if pbar:
                        pbar.update(len(batch))
                    yield batch
                    batch = []

        # Yield remaining emails
        if batch:
            processed_count += len(batch)
            if pbar:
                pbar.update(len(batch))
            yield batch
    finally:
        if pbar:
            pbar.close()
        if show_progress:
            logger.info(f"Processed {processed_count} emails")


