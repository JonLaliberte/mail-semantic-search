"""Tests for the incremental scan's change-time cutoff."""

import os
from datetime import datetime, timedelta

import pytest

from mail_semantic_search.mailmate_reader import (
    _scan_eml_files_find,
    _scan_eml_files_python,
)


def _moved_file(tmp_path):
    """Simulate MailMate archiving a Gmail message: the .eml is renamed from
    INBOX to All Mail with its mtime preserved, so only its ctime is recent."""
    inbox = tmp_path / "INBOX.mailbox" / "Messages"
    all_mail = tmp_path / "[Gmail].mailbox" / "All Mail.mailbox" / "Messages"
    inbox.mkdir(parents=True)
    all_mail.mkdir(parents=True)

    old = inbox / "1424151.eml"
    old.write_text("Message-ID: <m@x>\n\nbody\n")
    month_ago = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(old, (month_ago, month_ago))

    moved = all_mail / "1424151.eml"
    os.rename(old, moved)
    return moved


@pytest.mark.parametrize("scan", [_scan_eml_files_find, _scan_eml_files_python])
def test_incremental_scan_finds_moved_file_with_old_mtime(tmp_path, scan):
    moved = _moved_file(tmp_path)
    cutoff = datetime.now() - timedelta(days=1)

    assert moved in list(scan(tmp_path, cutoff))


@pytest.mark.parametrize("scan", [_scan_eml_files_find, _scan_eml_files_python])
def test_incremental_scan_skips_file_unchanged_since_cutoff(tmp_path, scan):
    _moved_file(tmp_path)
    cutoff = datetime.now() + timedelta(minutes=1)

    assert list(scan(tmp_path, cutoff)) == []
