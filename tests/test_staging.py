"""Tests for staging.stage_email + clear_staged."""

import asyncio
import base64
import hashlib
import os
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import pytest
from fastmcp import Client

from mail_semantic_search.config import config
from mail_semantic_search.database import Database, get_file_hash
from mail_semantic_search.mailmate_reader import extract_attachments
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
            return await client.call_tool(
                "get_email_attachments",
                {"message_id": "stage-test-001@example.com", "attachment_ids": [ids[0]]},
            )

    result = asyncio.run(call())
    assert result.data["status"] == "ok"
    assert base64.b64decode(result.data["attachments"][0]["data_base64"]) == b"abcd"
    assert not config.staging_dir.exists()
