"""macOS-only actions against MailMate.

Opens use the macOS `open` command (LaunchServices) — fast, fire-and-forget,
no AppleEvent round-trip. The previous AppleScript `open location` approach
hung for the full 120s AppleScript timeout because the tell-block waited for
MailMate to ack the URL dispatch, which it doesn't reliably do while it's
busy actually loading the message.

`perform` actions run as a separate osascript call AFTER a brief delay so
MailMate has time to bring the message into focus. The AppleScript itself
uses `with timeout of N seconds` so even if MailMate is slow we fail fast
rather than waiting 120s.

Selectors come from MailMate's bundled keybinding plists
(`/Applications/MailMate.app/Contents/Resources/KeyBindings/{Gmail,Standard}.plist`):

  * Mark read   →  setTag: \\Seen          (the IMAP \\Seen flag)
  * Mark unread →  removeTag: \\Seen
  * Archive     →  archive:                (no argument)

`perform` accepts a flat list of alternating selectors and arguments.
"""

from __future__ import annotations

import subprocess
import time
from typing import Dict, List
from urllib.parse import quote


# How long to wait after dispatching the open URL before sending perform.
# MailMate needs to load the message and bring it to focus; perform runs
# against the current first responder.
_OPEN_TO_PERFORM_DELAY_SECONDS = 0.6

# AppleScript-level timeout on the perform call. If MailMate is wedged we
# return quickly with a clear status instead of waiting the AppleScript
# default of 120s.
_PERFORM_APPLESCRIPT_TIMEOUT_SECONDS = 8

# subprocess.run timeout — must be larger than the AppleScript-level timeout
# above so AppleScript can finish its own error path before we kill osascript.
_OSASCRIPT_SUBPROCESS_TIMEOUT_SECONDS = 15


def _normalize_message_id(message_id: str) -> str:
    """Strip RFC-822 angle brackets and URL-encode the remaining id."""
    cleaned = message_id.strip()
    if cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1]
    # Don't encode '@' — it's valid in message-id URLs and MailMate expects it bare.
    return quote(cleaned, safe="@.-_+/")


def _dispatch_open(url: str, background: bool = False) -> Dict[str, object]:
    """Dispatch a URL via macOS `open` (LaunchServices). Returns instantly.

    background=True passes `-g`, which tells LaunchServices not to bring the
    receiving app to the foreground. Use this for action tools (mark read,
    archive) where the user just wants the side effect, not a window pop-up.
    """
    cmd = ["open"]
    if background:
        cmd.append("-g")
    cmd.append(url)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "stdout": "", "stderr": "open timed out after 5s"}
    except FileNotFoundError:
        return {
            "status": "failed",
            "stdout": "",
            "stderr": "`open` not found — these tools require macOS",
        }

    if result.returncode != 0:
        return {
            "status": "failed",
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip() or f"open exited {result.returncode}",
        }
    return {"status": "ok", "stdout": "", "stderr": ""}


def _run_osascript(script: str) -> Dict[str, object]:
    """Run an AppleScript and return {status, stdout, stderr}."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=_OSASCRIPT_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "stdout": "",
            "stderr": f"osascript timed out after {_OSASCRIPT_SUBPROCESS_TIMEOUT_SECONDS}s",
        }
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
    return {"status": "ok", "stdout": result.stdout.strip(), "stderr": ""}


def _build_selector_list(selectors: List[str]) -> str:
    """Build an AppleScript list literal: {"setTag:", "\\Seen", "archive:"}."""
    parts = []
    for sel in selectors:
        # Escape backslashes and quotes for AppleScript string literal.
        escaped = sel.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'"{escaped}"')
    return "{" + ", ".join(parts) + "}"


def _perform_on_message(message_id: str, selectors: List[str]) -> Dict[str, object]:
    """Bring the message into focus and invoke a chain of selectors against it.

    Two-step dispatch, both intentionally backgrounded:
      1. macOS `open -g message://...` — dispatches the URL via
         LaunchServices but does NOT foreground MailMate. MailMate still
         processes the URL (and a viewer window may open), but the user's
         current app stays focused.
      2. Brief delay, then a separate osascript that calls `perform`
         (without `activate`, again to avoid foregrounding). AppleScript
         timeout caps the wait.
    """
    normalized = _normalize_message_id(message_id)

    open_result = _dispatch_open(f"message:{normalized}", background=True)
    if open_result["status"] != "ok":
        open_result["message_id"] = message_id
        open_result["step"] = "open"
        return open_result

    time.sleep(_OPEN_TO_PERFORM_DELAY_SECONDS)

    selector_list = _build_selector_list(selectors)
    script = (
        f'with timeout of {_PERFORM_APPLESCRIPT_TIMEOUT_SECONDS} seconds\n'
        f'    tell application "MailMate"\n'
        f'        perform {selector_list}\n'
        f'    end tell\n'
        f'end timeout'
    )
    result = _run_osascript(script)
    result["message_id"] = message_id
    result["step"] = "perform"
    return result


def open_email(message_id: str) -> Dict[str, object]:
    """Open the given message in MailMate (foregrounds the app — the user asked to see it)."""
    normalized = _normalize_message_id(message_id)
    result = _dispatch_open(f"message:{normalized}", background=False)
    result["message_id"] = message_id
    return result


def mark_email_read(message_id: str) -> Dict[str, object]:
    """Set the \\Seen flag on the given message in MailMate."""
    return _perform_on_message(message_id, ["setTag:", "\\Seen"])


def archive_email(message_id: str) -> Dict[str, object]:
    """Invoke MailMate's archive: action on the given message."""
    return _perform_on_message(message_id, ["archive:"])


def mark_read_and_archive(message_id: str) -> Dict[str, object]:
    """Mark as read AND archive in one MailMate trip — natural triage finisher."""
    return _perform_on_message(message_id, ["setTag:", "\\Seen", "archive:"])
