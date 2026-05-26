"""Convert HTML email bodies into clean plain text for indexing/search.

The previous implementation in mailmate_reader.parse_email_file used a regex
to strip tags, which left CSS rules, <script> contents, and HTML entities in
the indexed body — polluting both search snippets and embedding vectors.

This module replaces that with a BS4 cleanup pass followed by html2text
markdown conversion, plus a footer-marker truncation to drop unsubscribe
boilerplate.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, List

from bs4 import BeautifulSoup, Tag
import html2text

logger = logging.getLogger(__name__)

# Tags whose subtree is pure noise for an indexer.
_DROP_TAGS = ("style", "script", "head", "meta", "link", "title", "noscript")

# Substrings that mark the start of unsubscribe / footer boilerplate. Match is
# case-insensitive AND line-anchored (marker must start a line, possibly after
# leading whitespace or markdown table glyphs). Anchoring is the primary
# protection against gutting transactional emails — "Click [here] to view in
# browser" or "...help me unsubscribe from this list" appear mid-sentence and
# must not trigger truncation. The min-body-chars threshold below is the
# secondary defense.
#
# Markers we deliberately do NOT include:
#   - "view in browser" / "view this email": almost always preamble links
#     ("Having trouble viewing this email?"), never footers
_FOOTER_MARKERS = (
    "unsubscribe",
    "update your preferences",
    "you are receiving this email because",
    "manage your subscription",
)
_FOOTER_MIN_BODY_CHARS = 200
# Match: start-of-string OR newline, then optional whitespace/markdown glyphs
# (`|`, `>`, `*`, `-`, `[`), then the marker. Pre-built per-marker for speed.
_FOOTER_MARKER_RES = tuple(
    re.compile(rf"(?im)(?:^|\n)[\s>|*\-\[]*{re.escape(m)}")
    for m in _FOOTER_MARKERS
)

# Inline styles that hide an element.
_HIDDEN_STYLE_RE = re.compile(
    r"(?i)(display\s*:\s*none|visibility\s*:\s*hidden)"
)

# Class names that commonly mark preheader/hidden text in marketing templates.
_PREHEADER_CLASS_HINTS = ("preheader", "preview-text", "hidden", "screen-reader")


def _drop_hidden(soup: BeautifulSoup) -> None:
    """Remove elements styled as hidden, plus common preheader containers."""
    for el in list(soup.find_all(attrs={"style": True})):
        if not isinstance(el, Tag) or not getattr(el, "attrs", None):
            continue
        style = el.attrs.get("style") or ""
        if isinstance(style, list):
            style = " ".join(style)
        if _HIDDEN_STYLE_RE.search(style):
            el.decompose()

    for el in list(soup.find_all(attrs={"class": True})):
        if not isinstance(el, Tag) or not getattr(el, "attrs", None):
            continue
        classes = el.attrs.get("class") or []
        if isinstance(classes, str):
            classes = [classes]
        lowered = " ".join(c.lower() for c in classes)
        if any(hint in lowered for hint in _PREHEADER_CLASS_HINTS):
            el.decompose()


def _replace_images_with_alt(soup: BeautifulSoup) -> None:
    """Replace <img> with its alt text if present, else drop entirely."""
    for img in list(soup.find_all("img")):
        if not isinstance(img, Tag):
            continue
        alt = (img.get("alt") or "").strip()
        if alt:
            img.replace_with(alt)
        else:
            img.decompose()


def _truncate_at_footer_marker(text: str) -> str:
    """Drop everything from the earliest line-anchored footer marker onward.

    Line-anchored matching means a marker word in mid-sentence (e.g. "Click
    here to unsubscribe" or "Having trouble viewing this email?") will NOT
    trigger truncation. Only markers that begin a line — typically separated
    footers like `Unsubscribe | Manage preferences` — fire.

    Secondary defense: the marker must appear at least _FOOTER_MIN_BODY_CHARS
    into the body so a one-line message that IS just "Unsubscribe" survives.
    """
    earliest = -1
    for pat in _FOOTER_MARKER_RES:
        m = pat.search(text)
        if m is None:
            continue
        idx = m.start()
        if idx < _FOOTER_MIN_BODY_CHARS:
            continue
        if earliest == -1 or idx < earliest:
            earliest = idx

    if earliest == -1:
        return text

    # Truncate at the start of the line containing the marker so we don't
    # leave a half-sentence.
    line_start = text.rfind("\n", 0, earliest)
    cut = line_start + 1 if line_start >= 0 else earliest
    return text[:cut].rstrip()


_REDUNDANT_LINK_RE = re.compile(r"\[(https?://[^\]]+)\]\(\1\)")
_BLANK_RUNS_RE = re.compile(r"\n{3,}")


def _build_converter() -> html2text.HTML2Text:
    h = html2text.HTML2Text()
    h.body_width = 0          # no line wrapping
    h.ignore_images = True    # we've already replaced/decomposed them
    h.ignore_emphasis = True  # drop *bold* markers
    h.single_line_break = True
    # Keep links — default format is [text](url), which is what we want.
    return h


def html_to_text(html: str) -> str:
    """Convert email-body HTML to clean plain text.

    Steps: parse with lxml -> drop style/script/head/etc. -> drop hidden /
    preheader elements -> replace <img> with alt text -> html2text markdown
    -> collapse redundant link syntax -> truncate at footer markers ->
    squeeze blank runs.
    """
    if not html:
        return ""

    try:
        return _html_to_text_inner(html)
    except Exception as e:
        # A single malformed email must not crash a 400k-row backfill.
        logger.warning("html_to_text: pipeline failed (%s); falling back to raw HTML text", e)
        try:
            return BeautifulSoup(html, "lxml").get_text("\n").strip()
        except Exception:
            return html


def _html_to_text_inner(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    for tag_name in _DROP_TAGS:
        for el in list(soup.find_all(tag_name)):
            el.decompose()

    _drop_hidden(soup)
    _replace_images_with_alt(soup)

    cleaned_html = str(soup)

    converter = _build_converter()
    try:
        markdown = converter.handle(cleaned_html)
    except Exception as e:
        logger.debug("html_to_text: html2text conversion failed (%s); falling back to soup.get_text", e)
        markdown = soup.get_text("\n")

    markdown = _REDUNDANT_LINK_RE.sub(r"\1", markdown)
    markdown = _truncate_at_footer_marker(markdown)
    markdown = _BLANK_RUNS_RE.sub("\n\n", markdown)
    return markdown.strip()
