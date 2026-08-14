"""MailMate deep links derived from an RFC-822 Message-ID.

Every email result the MCP server returns carries ready-to-paste links back
to the message in MailMate, so consumers never re-implement URL encoding.
MailMate resolves `message:` and `mid:` URLs off the `Message-ID` header
(https://manual.mailmate-app.com/extended_url_scheme).

Two encoding gotchas drive the rules here:

  * A literal `%` in a Message-ID breaks `message:` URLs outright, so the
    bracketed form is percent-encoded exactly once. `/` is left bare — it is
    valid in the opaque part and appears in real IDs (GitHub notifications).
  * `(`, `)`, and `]` break Markdown link syntax. `markdown_link` strips
    brackets from the label and falls back to the encoded `message:` target
    when the bare `mid:` form contains parens.

`mid_url` is deliberately unencoded per RFC 2392; `message_url` is always
emitted alongside it as the unambiguous option.
"""

from typing import Dict, Optional
from urllib.parse import quote

from mail_semantic_search.config import config

# Kept bare in `message:` URLs. `@` stays readable and `/` is valid in the
# opaque part — encoding it gains nothing and hurts legibility.
_MESSAGE_URL_SAFE_CHARS = "@/"

_NO_SUBJECT_LABEL = "(no subject)"


def normalize_message_id(message_id: Optional[str]) -> Optional[str]:
    """Strip whitespace and any surrounding angle brackets. None if empty."""
    if not message_id:
        return None
    mid = message_id.strip()
    if mid.startswith("<") and mid.endswith(">"):
        mid = mid[1:-1]
    mid = mid.strip()
    return mid or None


def _markdown_label(subject: Optional[str]) -> str:
    """Build a Markdown-safe link label from a subject line."""
    label = (subject or _NO_SUBJECT_LABEL).replace("[", "").replace("]", "")
    # Collapse newlines/tabs left over from folded headers.
    label = " ".join(label.split())
    return label or _NO_SUBJECT_LABEL


def build_link_fields(
    message_id: Optional[str],
    subject: Optional[str] = None,
    scheme: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Return the `message_url` / `mid_url` / `markdown_link` triple.

    All three are None when there is no usable Message-ID — callers get a
    consistent shape rather than a missing key or a broken URL. `scheme`
    defaults to the process-wide `MAILMATE_LINK_SCHEME` setting.
    """
    mid = normalize_message_id(message_id)
    if mid is None:
        return {"message_url": None, "mid_url": None, "markdown_link": None}

    # quote() runs exactly once: a literal `%25` in the header is a literal
    # percent-two-five and must become `%2525`. Never pre-clean the id.
    message_url = "message:" + quote(f"<{mid}>", safe=_MESSAGE_URL_SAFE_CHARS)
    mid_url = f"mid:{mid}"

    target = mid_url if (scheme or config.link_scheme) == "mid" else message_url
    # A paren in the target closes the Markdown link early. The encoded
    # `message:` form escapes them, so prefer it over emitting broken syntax.
    if "(" in target or ")" in target:
        target = message_url

    return {
        "message_url": message_url,
        "mid_url": mid_url,
        "markdown_link": f"[{_markdown_label(subject)}]({target})",
    }


def attach_link_fields(result: Dict) -> Dict:
    """Return a copy of an email result with the deep-link fields added."""
    enriched = dict(result)
    enriched.update(
        build_link_fields(result.get("message_id"), result.get("subject"))
    )
    return enriched
