"""Tripwire: CURRENT_EXTRACTION_VERSION must have a matching changelog entry.

Forces anyone bumping the constant to also append a `#   N — YYYY-MM-DD:`
line so future-us can answer "why is the index re-extracting?".
"""

import inspect
import re

from mail_semantic_search import mailmate_reader


def test_extraction_version_is_positive_int():
    v = mailmate_reader.CURRENT_EXTRACTION_VERSION
    assert isinstance(v, int)
    assert v >= 1


def test_extraction_version_has_changelog_entry():
    """A `#   N — YYYY-MM-DD:` line for the current version must exist."""
    source = inspect.getsource(mailmate_reader)
    versions = {
        int(m.group(1))
        for m in re.finditer(
            r"#\s+(\d+)\s+—\s+\d{4}-\d{2}-\d{2}:", source
        )
    }
    assert mailmate_reader.CURRENT_EXTRACTION_VERSION in versions, (
        f"CURRENT_EXTRACTION_VERSION={mailmate_reader.CURRENT_EXTRACTION_VERSION} "
        f"but no matching changelog line found. Versions in changelog: {sorted(versions)}. "
        "Add a `#   N — YYYY-MM-DD: <reason>` line above the constant."
    )
