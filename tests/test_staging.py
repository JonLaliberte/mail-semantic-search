"""Tests for staging.stage_email + clear_staged."""

import asyncio
import base64
import hashlib
import json
import os
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

from mail_semantic_search.config import config
from mail_semantic_search.database import Database, get_file_hash
from mail_semantic_search.mailmate_reader import extract_attachments, parse_email_file
from mail_semantic_search.staging import fetch_attachments, stage_email


def _build_eml_with_attachments(path: Path, attachments: list[tuple[str, str, bytes]]) -> None:
    """Build a minimal multipart .eml file at `path`.

    attachments: list of (filename, content_type, data_bytes).
    """
    msg = EmailMessage()
    msg["Subject"] = "Test stage"
    msg["From"] = "sender@example.com"
    msg["To"] = "rcpt@example.com"
    msg["Message-ID"] = "<stage-test-001@example.com>"
    msg.set_content("Hello, this is the body.")
    for filename, content_type, data in attachments:
        maintype, _, subtype = content_type.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    path.write_bytes(bytes(msg))


def _index_email(db: Database, file_path: str, message_id: str = "<stage-test-001@example.com>",
                 *, attachments=None) -> int:
    if attachments is None:
        attachments = [
            {"filename": "report.pdf", "content_type": "application/pdf", "size": 100, "content_disposition": "attachment"},
        ]
    return db.add_email(
        {
            "file_path": file_path,
            "message_id": message_id,
            "subject": "Test stage",
            "from": "sender@example.com",
            "to": "rcpt@example.com",
            "cc": "",
            "bcc": "",
            "date": "2024-01-01",
            "body": "Hello, this is the body.",
            "in_reply_to": "",
            "references": "",
        },
        attachments=attachments,
        file_mtime=1000.0,
    )


def test_stage_copies_attachments_and_eml(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.staging import stage_email

    eml_path = tmp_path / "msg.eml"
    pdf_bytes = b"%PDF-1.4 fake pdf body"
    png_bytes = b"\x89PNG fake png body"
    _build_eml_with_attachments(eml_path, [
        ("invoice.pdf", "application/pdf", pdf_bytes),
        ("logo.png", "image/png", png_bytes),
    ])

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "stage.db")
    monkeypatch.setattr(cfg_mod.config, "staging_dir", tmp_path / "staged")

    db = Database()
    _index_email(db, str(eml_path))
    db.close()

    result = stage_email(file_path=str(eml_path))
    assert result["status"] == "ok"
    staged_dir = Path(result["staged_dir"])
    assert staged_dir.exists()
    assert Path(result["eml_path"]).exists()
    assert Path(result["eml_path"]).read_bytes() == eml_path.read_bytes()

    by_name = {Path(a["path"]).name: a for a in result["attachments"]}
    assert "invoice.pdf" in by_name
    assert "logo.png" in by_name
    assert Path(by_name["invoice.pdf"]["path"]).read_bytes() == pdf_bytes
    assert Path(by_name["logo.png"]["path"]).read_bytes() == png_bytes


def test_stage_idempotent_same_hash(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.staging import stage_email

    eml_path = tmp_path / "msg.eml"
    _build_eml_with_attachments(eml_path, [("a.pdf", "application/pdf", b"hello")])

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "stage2.db")
    monkeypatch.setattr(cfg_mod.config, "staging_dir", tmp_path / "staged2")

    db = Database()
    _index_email(db, str(eml_path))
    db.close()

    first = stage_email(file_path=str(eml_path))
    second = stage_email(file_path=str(eml_path))
    assert first["staged_dir"] == second["staged_dir"]
    # The expected short_hash matches what staging uses internally.
    expected_short = get_file_hash(str(eml_path))[:12]
    assert Path(first["staged_dir"]).name == expected_short


def test_stage_by_message_id_with_and_without_brackets(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.staging import stage_email

    eml_path = tmp_path / "msg.eml"
    _build_eml_with_attachments(eml_path, [("a.txt", "text/plain", b"hi")])

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "stage3.db")
    monkeypatch.setattr(cfg_mod.config, "staging_dir", tmp_path / "staged3")

    db = Database()
    _index_email(db, str(eml_path), message_id="<stage-test-001@example.com>")
    db.close()

    r1 = stage_email(message_id="<stage-test-001@example.com>")
    assert r1["status"] == "ok"
    r2 = stage_email(message_id="stage-test-001@example.com")  # no brackets
    assert r2["status"] == "ok"
    assert r1["staged_dir"] == r2["staged_dir"]


def test_stage_not_indexed_returns_friendly_status(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.staging import stage_email

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "stage4.db")
    monkeypatch.setattr(cfg_mod.config, "staging_dir", tmp_path / "staged4")
    Database().close()  # init schema

    result = stage_email(file_path=str(tmp_path / "nope.eml"))
    assert result["status"] == "not_indexed"
    assert result["staged_dir"] == ""


def test_stage_source_missing(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.staging import stage_email

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "stage5.db")
    monkeypatch.setattr(cfg_mod.config, "staging_dir", tmp_path / "staged5")

    db = Database()
    fake_path = str(tmp_path / "deleted.eml")
    _index_email(db, fake_path)  # row exists but file doesn't
    db.close()

    result = stage_email(file_path=fake_path)
    assert result["status"] == "source_missing"


def test_stage_unsafe_filename_is_sanitized(tmp_path, monkeypatch):
    """Attachments with shell-special chars in their filename should be safe to read."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.staging import stage_email

    eml_path = tmp_path / "msg.eml"
    _build_eml_with_attachments(eml_path, [
        ("../etc/passwd; rm -rf /.pdf", "application/pdf", b"sneaky"),
    ])

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "stage6.db")
    monkeypatch.setattr(cfg_mod.config, "staging_dir", tmp_path / "staged6")

    db = Database()
    _index_email(db, str(eml_path))
    db.close()

    result = stage_email(file_path=str(eml_path))
    assert result["status"] == "ok"
    out_path = Path(result["attachments"][0]["path"])
    # No path traversal: staged file must stay under the per-email dir.
    out_path.resolve().relative_to(Path(result["staged_dir"]).resolve())


def test_stage_no_eml(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.staging import stage_email

    eml_path = tmp_path / "msg.eml"
    _build_eml_with_attachments(eml_path, [("a.txt", "text/plain", b"hi")])

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "stage7.db")
    monkeypatch.setattr(cfg_mod.config, "staging_dir", tmp_path / "staged7")

    db = Database()
    _index_email(db, str(eml_path))
    db.close()

    result = stage_email(file_path=str(eml_path), include_eml=False)
    assert result["status"] == "ok"
    assert result["eml_path"] is None
    assert len(result["attachments"]) == 1


def test_clear_staged_single_and_all(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.staging import stage_email, clear_staged

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "stage8.db")
    monkeypatch.setattr(cfg_mod.config, "staging_dir", tmp_path / "staged8")

    # Stage two emails
    db = Database()
    paths = []
    for i, mid in enumerate(["<a@x>", "<b@x>"]):
        eml = tmp_path / f"msg{i}.eml"
        _build_eml_with_attachments(eml, [("a.txt", "text/plain", b"hi")])
        _index_email(db, str(eml), message_id=mid)
        r = stage_email(file_path=str(eml))
        paths.append(Path(r["staged_dir"]))
    db.close()
    assert all(p.exists() for p in paths)

    # Clear just one
    short = paths[0].name
    res = clear_staged(short_hash=short)
    assert res["removed"] == 1
    assert not paths[0].exists()
    assert paths[1].exists()

    # Clear all
    res = clear_staged()
    assert res["removed"] == 1
    assert not paths[1].exists()


def test_clear_staged_rejects_path_traversal(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.staging import clear_staged

    monkeypatch.setattr(cfg_mod.config, "staging_dir", tmp_path / "staged9")
    (tmp_path / "staged9").mkdir()
    # A neighbor file outside the staging dir that must NOT be deletable.
    neighbor = tmp_path / "important.txt"
    neighbor.write_text("keep me")

    with pytest.raises(ValueError):
        clear_staged(short_hash="../important.txt")

    assert neighbor.exists()


@pytest.fixture
def indexed_message(tmp_path, monkeypatch):
    maildir = tmp_path / "mail"
    maildir.mkdir()
    monkeypatch.setattr(config, "email_dir", maildir)
    monkeypatch.setattr(config, "staging_dir", tmp_path / "staged")
    monkeypatch.setattr(config, "database_path", tmp_path / "metadata #1.db")
    source = maildir / "message.eml"
    _build_eml_with_attachments(source, [
        ("résumé.txt", "text/plain", b"abcd"),
        ("résumé.txt", "text/plain", b"efgh"),
        ("large.pdf", "application/pdf", b"12345678"),
        ("tiny.txt", "text/plain", b"x"),
    ])
    message = BytesParser(policy=policy.default).parsebytes(source.read_bytes())
    # Mail can supply Content-Length. The index retains it, while response
    # metadata and caps must reflect the actual decoded size.
    message.get_payload()[3]["Content-Length"] = "2"
    source.write_bytes(message.as_bytes())
    with Database() as database:
        email_id = _index_email(
            database, str(source), attachments=extract_attachments(message, extract_text=False)
        )
        ids = [entry["id"] for entry in database.get_attachments(email_id)]
    return source, ids


def test_fetch_bytes_hash_and_names_match_staging(indexed_message):
    source, ids = indexed_message
    result = fetch_attachments(message_id="stage-test-001@example.com", include_eml=True)
    assert result["status"] == "ok"
    assert result["message_id"] == "<stage-test-001@example.com>"
    assert result["file_path"] == str(source)
    assert not config.staging_dir.exists()
    assert [entry["id"] for entry in result["attachments"]] == ids
    assert base64.b64decode(result["eml"]["data_base64"]) == source.read_bytes()
    assert result["eml"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()

    staged = stage_email(file_path=str(source), include_eml=False)
    for fetched, copied in zip(result["attachments"], staged["attachments"], strict=True):
        data = Path(copied["path"]).read_bytes()
        assert fetched["filename"] == copied["filename"]
        assert fetched["safe_name"] == Path(copied["path"]).name
        assert fetched["content_type"] == copied["content_type"]
        assert fetched["size"] == len(data)
        assert fetched["sha256"] == hashlib.sha256(data).hexdigest()
        assert fetched["skipped"] is None
        assert base64.b64decode(fetched["data_base64"]) == data


def test_fetch_selects_ids_in_mime_order_and_intersects_filenames(indexed_message):
    source, ids = indexed_message
    result = fetch_attachments(
        file_path=str(source),
        message_id="<stage-test-001@example.com>",
        attachment_ids=[ids[1], ids[2], 999],
        filenames=["résumé.txt"],
    )
    assert [entry["id"] for entry in result["attachments"]] == [ids[1]]
    assert base64.b64decode(result["attachments"][0]["data_base64"]) == b"efgh"
    assert result["unmatched_attachment_ids"] == [999]
    assert result["eml"] is None
    assert fetch_attachments(file_path=str(source), attachment_ids=[])["attachments"] == []


def test_fetch_uses_the_attachment_ids_for_the_exact_indexed_copy(indexed_message):
    source, ids = indexed_message
    copy = source.with_name("copy.eml")
    copy.write_bytes(source.read_bytes())
    message = BytesParser(policy=policy.default).parsebytes(copy.read_bytes())
    attachments = extract_attachments(message, extract_text=False)
    with Database() as database:
        copy_id = _index_email(database, str(copy), attachments=attachments)
        before = database.get_attachments(copy_id)[0]["id"]
        _index_email(database, str(copy), attachments=attachments)
        after = database.get_attachments(copy_id)[0]["id"]

    result = fetch_attachments(
        message_id="stage-test-001@example.com", file_path=str(copy),
        attachment_ids=[ids[0], before, after],
    )
    assert [entry["id"] for entry in result["attachments"]] == [after]
    assert result["unmatched_attachment_ids"] == sorted([ids[0], before])


def test_fetch_clamps_caps_and_keeps_trying_later_files(indexed_message, monkeypatch):
    source, _ = indexed_message
    monkeypatch.setattr(config, "max_attachment_fetch_bytes", 5)
    monkeypatch.setattr(config, "max_attachment_fetch_total", 6)
    result = fetch_attachments(
        file_path=str(source), max_bytes_per_file=99, max_total_bytes=5, include_eml=True
    )
    parts = result["attachments"]
    assert [part["skipped"] for part in parts] == [None, "over_total", "too_large", None]
    assert parts[2]["size"] == 8
    assert parts[2]["data_base64"] is None
    assert parts[2]["sha256"] is None
    assert result["eml"]["skipped"] == "too_large"

    result = fetch_attachments(file_path=str(source), max_total_bytes=99)
    assert [part["skipped"] for part in result["attachments"]] == [None, "over_total", "too_large", None]
    result = fetch_attachments(
        file_path=str(source), max_bytes_per_file=source.stat().st_size,
        include_eml=True, attachment_ids=[],
    )
    assert result["eml"]["data_base64"] is None
    with pytest.raises(ValueError, match="nonnegative"):
        fetch_attachments(file_path=str(source), max_total_bytes=-1)


def test_fetch_eml_uses_budget_left_after_attachments(indexed_message):
    source, _ = indexed_message
    result = fetch_attachments(
        file_path=str(source), include_eml=True, max_total_bytes=source.stat().st_size
    )
    assert result["eml"]["skipped"] == "over_total"
    assert result["eml"]["size"] == source.stat().st_size
    assert result["eml"]["data_base64"] is None


def test_fetch_reports_an_undecodable_part(indexed_message):
    source, _ = indexed_message
    message = BytesParser(policy=policy.default).parsebytes(source.read_bytes())
    message.get_payload()[1].set_payload("A")  # Invalid base64 length.
    source.write_bytes(message.as_bytes())

    result = fetch_attachments(file_path=str(source))
    unreadable = result["attachments"][0]
    assert unreadable["skipped"] == "unreadable"
    assert unreadable["data_base64"] is None
    assert result["attachments"][1]["skipped"] is None


def test_stage_keeps_invalid_base64_raw_fallback(indexed_message):
    source, _ = indexed_message
    message = BytesParser(policy=policy.default).parsebytes(source.read_bytes())
    message.get_payload()[1].set_payload("A")  # A single base64 character cannot be decoded.
    source.write_bytes(message.as_bytes())

    staged = stage_email(file_path=str(source), include_eml=False)
    assert Path(staged["attachments"][0]["path"]).read_bytes() == b"A"


def test_fetch_decodes_only_selected_parts_that_fit(indexed_message, monkeypatch):
    from mail_semantic_search import staging

    source, ids = indexed_message
    original_decode = staging._decode_payload
    decoded = []

    def record_decode(part):
        decoded.append(part.get_filename())
        return original_decode(part)

    monkeypatch.setattr(staging, "_decode_payload", record_decode)
    fetch_attachments(file_path=str(source), attachment_ids=[ids[2], ids[3]], max_bytes_per_file=5)
    assert decoded == ["tiny.txt"]

    decoded.clear()
    fetch_attachments(file_path=str(source), max_bytes_per_file=5, max_total_bytes=5)
    assert decoded == ["résumé.txt", "tiny.txt"]

    decoded.clear()
    fetch_attachments(file_path=str(source), filenames=["large.pdf"], max_total_bytes=0)
    assert decoded == []


@pytest.mark.parametrize("report_small_size", [False, True], ids=["fstat", "grew-after-fstat"])
def test_fetch_rejects_oversize_source_before_parsing(indexed_message, monkeypatch, report_small_size):
    from mail_semantic_search import staging

    source, _ = indexed_message
    monkeypatch.setattr(config, "max_email_file_size", source.stat().st_size)
    with source.open("ab") as f:
        f.write(b"x" * 20)

    if report_small_size:
        # Simulate an append after fstat, with the real file backing the read.
        original_fstat = os.fstat

        def stale_fstat(fd):
            info = original_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_size=config.max_email_file_size)

        monkeypatch.setattr(staging.os, "fstat", stale_fstat)

    def unexpected_parse(*args, **kwargs):
        pytest.fail("Oversized source reached MIME parser")

    monkeypatch.setattr(staging.email, "message_from_binary_file", unexpected_parse)
    result = fetch_attachments(file_path=str(source), include_eml=True)
    assert result["status"] == "source_too_large"
    assert result["attachments"] == []
    assert result["eml"]["skipped"] == "too_large"
    assert result["eml"]["data_base64"] is None


def test_fetch_and_stage_safe_names_and_empty_part_parity(indexed_message):
    source, _ = indexed_message
    attachments = [
        ("../../etc/passwd", b"first"),
        ("Invoice.pdf", b"second"),
        ("invoice.pdf", b"third"),
        ("invoice-2.pdf", b"fourth"),
        (".bashrc", b"fifth"),
        ("...", b"sixth"),
        (". .notes", b"seventh"),
        (". .", b"eighth"),
        (". ..", b"ninth"),
        ("CON.txt", b"tenth"),
        ("cOm2 .notes", b"eleventh"),
        ("NUL", b"twelfth"),
        ("report.pdf. ", b"thirteenth"),
        ("empty.txt", b""),
    ]
    _build_eml_with_attachments(
        source, [(name, "application/octet-stream", data) for name, data in attachments]
    )
    fetched = fetch_attachments(file_path=str(source))["attachments"]
    staged = stage_email(file_path=str(source), include_eml=False)["attachments"]
    assert len(fetched) == len(staged) == len(attachments) - 1
    assert len({entry["safe_name"].casefold() for entry in fetched}) == len(fetched)
    assert [entry["safe_name"] for entry in fetched[-4:]] == [
        "_CON.txt", "_cOm2 .notes", "_NUL", "report.pdf",
    ]

    for entry, staged_entry, (original_name, data) in zip(
        fetched, staged, attachments[:-1], strict=True
    ):
        name = entry["safe_name"]
        assert entry["filename"] == original_name.rstrip()
        assert name and not name.startswith(".")
        assert "/" not in name and "\\" not in name
        assert Path(staged_entry["path"]).name == name
        assert Path(staged_entry["path"]).read_bytes() == data
        assert base64.b64decode(entry["data_base64"]) == data


def test_collision_truncation_does_not_leave_a_windows_reserved_suffix(indexed_message):
    source, _ = indexed_message
    long_name = "a." + "x" * 196 + " z"
    _build_eml_with_attachments(source, [
        (long_name, "application/octet-stream", b"first"),
        (long_name, "application/octet-stream", b"second"),
    ])
    fetched = fetch_attachments(file_path=str(source))["attachments"]
    assert len({entry["safe_name"].casefold() for entry in fetched}) == 2
    assert all(entry["safe_name"] == entry["safe_name"].rstrip(" .") for entry in fetched)
    assert all(len(entry["safe_name"]) <= 200 for entry in fetched)


@pytest.mark.parametrize(("encoding", "wire", "decoded"), [
    ("base64", b"QU\nJDag", b"ABCj"),
    ("quoted-printable", b"A=42=\r\nC==D=", b"ABC=D"),
    ("8bit", b"\xc3\xa9\xff", b"\xc3\xa9\xff"),
    ("x-uuencode", b"begin 644 note\n#04)#\n`\nend", b"ABC"),
])
def test_fetch_preflight_matches_transfer_encoding(indexed_message, encoding, wire, decoded):
    source, _ = indexed_message
    source.write_bytes(
        b'Content-Type: application/octet-stream\n'
        b'Content-Disposition: attachment; filename="example.bin"\n'
        + f"Content-Transfer-Encoding: {encoding}\n\n".encode("ascii")
        + wire
    )
    result = fetch_attachments(file_path=str(source), max_bytes_per_file=len(decoded))
    entry = result["attachments"][0]
    assert entry["size"] == len(decoded)
    assert base64.b64decode(entry["data_base64"]) == decoded


@pytest.mark.parametrize(("header", "wire"), [
    (b"base64 ", b"YWJj"),
    (b"quoted-printable (comment)", b"a=62c"),
])
def test_fetch_preflight_matches_index_for_noncanonical_encoding(indexed_message, header, wire):
    source, _ = indexed_message
    source.write_bytes(
        b'Content-Type: application/octet-stream\r\n'
        b'Content-Disposition: attachment; filename="encoding-test.bin"\r\n'
        b"Content-Transfer-Encoding: " + header + b"\r\n\r\n" + wire
    )
    message = BytesParser(policy=policy.default).parsebytes(source.read_bytes())
    expected = message.get_payload(decode=True)
    attachments = extract_attachments(message, extract_text=False)
    with Database() as database:
        email_id = _index_email(database, str(source), attachments=attachments)
        attachment_id = database.get_attachments(email_id)[0]["id"]

    result = fetch_attachments(
        file_path=str(source), attachment_ids=[attachment_id], max_bytes_per_file=len(expected)
    )
    assert result["unmatched_attachment_ids"] == []
    entry, = result["attachments"]
    assert entry["id"] == attachment_id
    assert entry["size"] == len(expected)
    assert base64.b64decode(entry["data_base64"]) == expected

    limited = fetch_attachments(file_path=str(source), max_bytes_per_file=len(expected) - 1)
    assert limited["attachments"][0]["skipped"] == "too_large"


def _write_crlf_message(source, attachments):
    lines = [
        b"From: Project Desk <sender@example.com>",
        b"To: Operations <rcpt@example.com>",
        b"Date: Sat, 28 Nov 2026 10:00:00 +0000",
        b"Subject: Schedule and notes",
        b"Message-ID: <schedule-test@example.com>",
        b"MIME-Version: 1.0",
        b'Content-Type: multipart/mixed; boundary="schedule-mime-boundary"',
        b"",
        b"--schedule-mime-boundary",
        b"Content-Type: text/plain; charset=us-ascii",
        b"",
        b"See the schedule notes attached.",
    ]
    for name, encoding, body in attachments:
        lines.extend([
            b"--schedule-mime-boundary",
            b"Content-Type: text/plain; charset=us-ascii",
            f'Content-Disposition: attachment; filename="{name}"'.encode("ascii"),
            b"Content-Transfer-Encoding: " + encoding,
            b"",
            body,
        ])
    lines.extend([b"--schedule-mime-boundary--", b""])
    source.write_bytes(b"\r\n".join(lines))


def test_fetch_uses_indexer_newline_handling_and_keeps_raw_crlf(indexed_message):
    source, _ = indexed_message
    _write_crlf_message(source, [("schedule.txt", b"7bit", b"Mon: Draft\r\nTue: Review")])
    message = parse_email_file(source)
    expected = b"Mon: Draft\nTue: Review"
    assert message["attachments"][0]["size"] == len(expected)
    with Database() as database:
        email_id = database.add_email(message, message["attachments"], file_mtime=1000.0)
        attachment_id = database.get_attachments(email_id)[0]["id"]

    result = fetch_attachments(file_path=str(source), attachment_ids=[attachment_id], include_eml=True)
    assert result["unmatched_attachment_ids"] == []
    entry, = result["attachments"]
    assert entry["id"] == attachment_id
    assert entry["size"] == len(expected)
    assert base64.b64decode(entry["data_base64"]) == expected
    assert base64.b64decode(result["eml"]["data_base64"]) == source.read_bytes()
    staged = stage_email(file_path=str(source), include_eml=False)
    assert Path(staged["attachments"][0]["path"]).read_bytes() == expected


@pytest.mark.parametrize("indexed_sizes", [[4, 3, 4, 5], [3, 3, 4, 3]])
def test_fetch_matches_ids_for_a_copied_index_across_decoder_versions(indexed_message, indexed_sizes):
    source, _ = indexed_message
    _write_crlf_message(source, [
        ("schedule.txt", b"base64 ", b"YWJj"),
        ("schedule.txt", b"7bit", b"Tue"),
        ("schedule.txt", b"7bit", b"Wed."),
        ("review.txt", b"quoted-printable (comment)", b"a=62c"),
    ])
    message = parse_email_file(source)
    # Record the sizes a 3.11 or 3.12+ indexer would have stored, with
    # same-name leaves whose sizes overlap across the two decoders.
    for attachment, size in zip(message["attachments"], indexed_sizes, strict=True):
        attachment["size"] = size
    with Database() as database:
        email_id = database.add_email(message, message["attachments"], file_mtime=1000.0)
        ids = [row["id"] for row in database.get_attachments(email_id)]

    result = fetch_attachments(file_path=str(source), attachment_ids=ids)
    staged = stage_email(file_path=str(source), include_eml=False)
    assert result["unmatched_attachment_ids"] == []
    assert [entry["id"] for entry in result["attachments"]] == ids
    assert [base64.b64decode(entry["data_base64"]) for entry in result["attachments"]] == [
        Path(entry["path"]).read_bytes() for entry in staged["attachments"]
    ]


@pytest.mark.parametrize(("indexed_size", "sibling"), [(3, b"XYZ"), (1, b"X")])
def test_fetch_does_not_give_unreadable_parts_ids_to_same_name_siblings(
    indexed_message, indexed_size, sibling
):
    source, _ = indexed_message
    _write_crlf_message(source, [
        ("receipt.txt", b"base64", b"YQ==Yg=="),
        ("receipt.txt", b"7bit", sibling),
    ])
    message = parse_email_file(source)
    # Python 3.14 records three bytes for the embedded-padding part,
    # whereas 3.12 records one. Exercise both index shapes on every runtime.
    message["attachments"][0]["size"] = indexed_size
    with Database() as database:
        email_id = database.add_email(message, message["attachments"], file_mtime=1000.0)
        ids = [row["id"] for row in database.get_attachments(email_id)]

    for attachment_id in ids:
        result = fetch_attachments(file_path=str(source), attachment_ids=[attachment_id])
        assert result["attachments"] == []
        assert result["unmatched_attachment_ids"] == [attachment_id]
        assert result["unmatched_attachment_id_reasons"] == {str(attachment_id): "ambiguous"}

    result = fetch_attachments(file_path=str(source), filenames=["receipt.txt"])
    unreadable, readable = result["attachments"]
    assert unreadable["id"] is readable["id"] is None
    assert unreadable["skipped"] == "unreadable"
    assert base64.b64decode(readable["data_base64"]) == sibling


def test_fetch_only_matches_unambiguous_ids_after_removing_a_same_name_part(indexed_message):
    source, _ = indexed_message
    attachments = [("receipt.txt", b"7bit", data) for data in (b"one", b"two", b"three")]
    _write_crlf_message(source, attachments)
    message = parse_email_file(source)
    with Database() as database:
        email_id = database.add_email(message, message["attachments"], file_mtime=1000.0)
        ids = [row["id"] for row in database.get_attachments(email_id)]
    _write_crlf_message(source, attachments[1:])

    for attachment_id in ids[:2]:
        result = fetch_attachments(file_path=str(source), attachment_ids=[attachment_id])
        assert result["attachments"] == []
        assert result["unmatched_attachment_ids"] == [attachment_id]
        assert result["unmatched_attachment_id_reasons"] == {str(attachment_id): "ambiguous"}

    result = fetch_attachments(file_path=str(source), attachment_ids=[ids[2]])
    entry, = result["attachments"]
    assert entry["id"] == ids[2]
    assert base64.b64decode(entry["data_base64"]) == b"three"


def test_fetch_raw_message_has_unique_basename(indexed_message):
    source, _ = indexed_message
    _build_eml_with_attachments(source, [
        ("Message.eml", "application/octet-stream", b"first"),
        ("MESSAGE-2.EML", "application/octet-stream", b"second"),
    ])

    result = fetch_attachments(file_path=str(source), include_eml=True)
    assert [entry["safe_name"] for entry in result["attachments"]] == ["Message.eml", "MESSAGE-2.EML"]
    assert result["eml"]["safe_name"] == "message-3.eml"
    assert base64.b64decode(result["eml"]["data_base64"]) == source.read_bytes()


def test_fetch_only_opens_indexed_eml_under_email_dir(indexed_message, tmp_path):
    source, _ = indexed_message
    outside = tmp_path / "mail-other" / "message.eml"
    outside.parent.mkdir()
    outside.write_bytes(source.read_bytes())
    link = source.parent / "linked.eml"
    link.symlink_to(outside)
    wrong_suffix = source.with_suffix(".txt")
    wrong_suffix.write_bytes(source.read_bytes())
    fifo = source.parent / "pipe.eml"
    os.mkfifo(fifo)
    unindexed = source.parent / "unindexed.eml"
    unindexed.write_bytes(source.read_bytes())
    with Database() as database:
        for path in (outside, link, wrong_suffix, fifo):
            _index_email(database, str(path))

    for path in (outside, link, wrong_suffix):
        assert fetch_attachments(file_path=str(path))["status"] == "source_outside_email_dir"
    assert fetch_attachments(file_path=str(fifo))["status"] == "source_unreadable"
    assert fetch_attachments(file_path=str(unindexed))["status"] == "not_indexed"
    assert fetch_attachments(file_path=str(source), message_id="other@example.com")["status"] == "not_indexed"
    source.unlink()
    assert fetch_attachments(file_path=str(source))["status"] == "source_missing"


def test_fetch_allows_email_dir_symlink(indexed_message, tmp_path, monkeypatch):
    source, _ = indexed_message
    alias = tmp_path / "archive"
    alias.symlink_to(source.parent, target_is_directory=True)
    monkeypatch.setattr(config, "email_dir", alias)
    with Database() as database:
        _index_email(database, str(alias / source.name), message_id="<alias@example.com>")
    result = fetch_attachments(message_id="alias@example.com")
    assert result["status"] == "ok"
    assert len(result["attachments"]) == 4


def test_fetch_through_mcp_without_opening_vector_store(indexed_message, monkeypatch):
    from mail_semantic_search import mcp_server

    _, ids = indexed_message

    def unexpected_vector_access():
        pytest.fail("Attachment fetch tried to open the vector store")

    monkeypatch.setattr(mcp_server.resources, "in_use", unexpected_vector_access)

    async def call():
        async with Client(mcp_server.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert tools["get_email_attachments"].annotations.readOnlyHint
            return await client.session.call_tool(
                "get_email_attachments",
                {"message_id": "stage-test-001@example.com", "attachment_ids": [ids[0]]},
            )

    result = asyncio.run(call())
    assert result.structuredContent["status"] == "ok"
    assert base64.b64decode(result.structuredContent["attachments"][0]["data_base64"]) == b"abcd"
    assert result.content == []
    assert not config.staging_dir.exists()


def test_fetch_mcp_serializes_three_mib_once(indexed_message):
    from mail_semantic_search.mcp_server import mcp

    source, _ = indexed_message
    payload = b"x" * (3 * 1024 * 1024)
    _build_eml_with_attachments(source, [("synthetic.bin", "application/octet-stream", payload)])

    async def call():
        async with Client(mcp) as client:
            return await client.session.call_tool("get_email_attachments", {"file_path": str(source)})

    result = asyncio.run(call())
    wire = result.model_dump_json(by_alias=True)
    base64_bytes = base64.b64encode(payload).decode("ascii")
    assert wire.count(base64_bytes) == 1
    assert len(wire) - len(base64_bytes) < 1024
    assert json.loads(wire)["content"] == []
