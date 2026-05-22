"""macOS-only AppleScript actions against MailMate.

These tools drive the running MailMate app via `osascript`. The MCP server
gates them behind a `platform.system() == "Darwin"` check at registration
time, so this module is only imported when AppleScript is actually usable.

Selectors used here come straight from MailMate's bundled keybinding plists
(`/Applications/MailMate.app/Contents/Resources/KeyBindings/{Gmail,Standard}.plist`):

  * Mark read   →  setTag: \\Seen          (the IMAP \\Seen flag)
  * Mark unread →  removeTag: \\Seen
  * Archive     →  archive:                (no argument)

`perform` accepts a flat list of alternating selectors and arguments, so
multi-step actions ride a single round-trip into MailMate.
"""

from __future__ import annotations

import subprocess
from typing import Dict, List
from urllib.parse import quote


def _normalize_message_id(message_id: str) -> str:
    """Strip RFC-822 angle brackets and URL-encode the remaining id."""
    cleaned = message_id.strip()
    if cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1]
    # Don't encode '@' — it's valid in message-id URLs and MailMate expects it bare.
    return quote(cleaned, safe="@.-_+")


def _run_osascript(script: str) -> Dict[str, object]:
    """Run an AppleScript and return {status, stdout, stderr}.

    Times out at 15s to keep a misbehaving MailMate from hanging the MCP.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "stdout": "", "stderr": "osascript timed out after 15s"}
    except FileNotFoundError:
        return {
            "status": "failed",
            "stdout": "",
            "stderr": "osascript not found — these tools require macOS",
        }

    if result.returncode != 0:
        return {
            "status": "failed",
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    return {"status": "ok", "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def _perform_on_message(message_id: str, selectors: List[str]) -> Dict[str, object]:
    """Open a message in MailMate and invoke a chain of selectors against it.

    The `open location` call selects the message in MailMate's UI; perform
    then operates on the current selection. The two run inside the same
    `tell` block so MailMate doesn't drop focus between them.
    """
    normalized = _normalize_message_id(message_id)
    # Build AppleScript list literal: {"setTag:", "\\Seen", "archive:"}
    # AppleScript strings are double-quoted; backslashes inside need escaping.
    parts = []
    for sel in selectors:
        # Escape any embedded quotes/backslashes for AppleScript string literal.
        escaped = sel.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'"{escaped}"')
    selector_list = "{" + ", ".join(parts) + "}"

    script = (
        f'tell application "MailMate"\n'
        f'    open location "message://{normalized}"\n'
        f'    perform {selector_list}\n'
        f'end tell'
    )
    result = _run_osascript(script)
    result["message_id"] = message_id
    return result


def open_email(message_id: str) -> Dict[str, object]:
    """Open the given message in MailMate (foregrounds the app)."""
    normalized = _normalize_message_id(message_id)
    script = f'tell application "MailMate" to open location "message://{normalized}"'
    result = _run_osascript(script)
    result["message_id"] = message_id
    return result


def mark_email_read(message_id: str) -> Dict[str, object]:
    """Set the \\Seen flag on the given message in MailMate."""
    return _perform_on_message(message_id, ["setTag:", "\\Seen"])


def archive_email(message_id: str) -> Dict[str, object]:
    """Invoke MailMate's archive: action on the given message."""
    return _perform_on_message(message_id, ["archive:"])


def mark_read_and_archive(message_id: str) -> Dict[str, object]:
    """Mark as read AND archive in one round-trip to MailMate."""
    return _perform_on_message(message_id, ["setTag:", "\\Seen", "archive:"])
