"""Tests for staging.stage_email + clear_staged."""

import base64
from email.message import EmailMessage
from pathlib import Path

import pytest

from mail_semantic_search.database import Database, get_file_hash


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


def _index_email(db: Database, file_path: str, message_id: str = "<stage-test-001@example.com>") -> int:
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
        attachments=[
            {"filename": "report.pdf", "content_type": "application/pdf", "size": 100, "content_disposition": "attachment"},
        ],
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
