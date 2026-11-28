import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import certifi
from fastmcp import Client
from fastmcp.exceptions import ToolError
import httpx
import pytest

from mail_semantic_search import attachment_stager


def part(safe_name, payload):
    return {
        "filename": safe_name, "safe_name": safe_name, "content_type": "text/plain",
        "size": len(payload), "data_base64": base64.b64encode(payload).decode(),
    }


@pytest.fixture
def stager(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("synthetic-test-token")
    result = {
        "status": "ok", "file_path": "/indexed/message.eml",
        "attachments": [part("receipt.txt", b"receipt text")],
        "eml": part("message.eml", b"test email"),
    }
    calls = []

    class Remote:
        def __init__(self, transport, *, timeout):
            assert str(transport.url) == "https://example.test/mcp"
            assert transport.httpx_client_factory.args[0].check_hostname and timeout == 120

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(structured_content=result)

    monkeypatch.setattr(attachment_stager, "Client", Remote)
    root = tmp_path.resolve() / "staged"
    server = attachment_stager.create_server(
        "https://example.test/mcp", token, Path(certifi.where()), root,
    )
    return Client(server), root, result, calls


def test_stages_selected_attachments_and_eml_without_returning_bytes(stager):
    client, root, result, calls = stager
    result["attachments"].append({
        **part("large.pdf", b"synthetic"), "skipped": "too_large", "data_base64": None,
    })
    result["unmatched_attachment_ids"] = [23]

    async def run():
        async with client:
            reply = await client.call_tool("stage_email_attachments", {
                "file_path": "/indexed/message.eml",
                "attachment_ids": [5, 23], "filenames": ["receipt.txt", "large.pdf"],
            })
            data = reply.structured_content
            assert data["status"] == "ok"
            assert data["file_path"] == "/indexed/message.eml"
            assert calls == [("get_email_attachments", {
                "message_id": None, "file_path": data["file_path"],
                "attachment_ids": [5, 23], "filenames": ["receipt.txt", "large.pdf"],
                "include_eml": True,
            })]
            staged = Path(data["staged_dir"])
            assert staged.parent == root
            attachment, = data["attachments"]
            assert attachment["size"] == 12
            assert Path(attachment["path"]).read_bytes() == b"receipt text"
            assert Path(data["eml_path"]).read_bytes() == b"test email"
            assert data["skipped"] == [{"filename": "large.pdf", "size": 9, "skipped": "too_large"}]
            assert data["unmatched_attachment_ids"] == [23]
            assert "data_base64" not in str(reply)
            irrelevant = root / "keep"
            irrelevant.mkdir()
            cleared = await client.call_tool("clear_staged_emails", {"short_hash": staged.name})
            assert cleared.structured_content["removed"] == 1
            assert not staged.exists() and irrelevant.is_dir()

    asyncio.run(run())


def test_unsafe_remote_filename_and_unsafe_clear_are_rejected(stager, tmp_path):
    client, root, result, _ = stager
    outside = tmp_path / "outside"
    outside.mkdir()
    result["attachments"] = [part("../../outside/created", b"escape")]

    async def run():
        async with client:
            root.mkdir()
            (root / "aaaaaaaaaaaa").symlink_to(outside, target_is_directory=True)
            with pytest.raises(ToolError, match="unsafe attachment name"):
                await client.call_tool("stage_email_attachments", {"message_id": "<test@example.invalid>"})
            with pytest.raises(ToolError, match="twelve-character"):
                await client.call_tool("clear_staged_emails", {"short_hash": "../outside"})
            cleared = await client.call_tool("clear_staged_emails", {})
            assert cleared.structured_content["removed"] == 1
            assert (root / "aaaaaaaaaaaa").is_symlink()
            assert list(outside.iterdir()) == []

    asyncio.run(run())


def test_existing_file_symlink_is_replaced_and_directory_symlink_is_rejected(stager, tmp_path):
    client, root, _, _ = stager
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")

    async def run():
        async with client:
            args = {"message_id": "<test@example.invalid>", "include_eml": False}
            first = (await client.call_tool("stage_email_attachments", args)).structured_content
            assert first["eml_path"] is None
            path = Path(first["attachments"][0]["path"])
            path.unlink()
            path.symlink_to(outside)
            await client.call_tool("stage_email_attachments", args)
            assert path.read_bytes() == b"receipt text" and not path.is_symlink()
            assert outside.read_bytes() == b"keep"
            path.unlink()
            path.parent.rmdir()
            path.parent.symlink_to(tmp_path, target_is_directory=True)
            with pytest.raises(ToolError, match="staging directory is unsafe"):
                await client.call_tool("stage_email_attachments", args)
            assert not (tmp_path / "receipt.txt").exists()
            assert Path(first["staged_dir"]).parent == root

    asyncio.run(run())


@pytest.mark.parametrize("root_kind", ["symlink", "writable", "foreign"])
def test_unsafe_root_blocks_writes_and_clearing(stager, tmp_path, monkeypatch, root_kind):
    client, root, _, _ = stager
    outside = tmp_path / "outside"
    outside.mkdir()
    child = outside / "aaaaaaaaaaaa"
    child.mkdir()
    kept = child / "keep"
    kept.write_bytes(b"keep")
    if root_kind == "symlink":
        root.symlink_to(outside, target_is_directory=True)
    else:
        outside.rename(root)
        kept = root / child.name / kept.name
        if root_kind == "writable":
            root.chmod(0o770)
        else:
            uid = root.stat().st_uid
            monkeypatch.setattr(attachment_stager.os, "geteuid", lambda: uid + 1)

    async def run():
        async with client:
            with pytest.raises(ToolError, match="staging directory is unsafe"):
                await client.call_tool("stage_email_attachments", {"file_path": "/indexed/message.eml"})
            with pytest.raises(ToolError, match="staging directory is unsafe"):
                await client.call_tool("clear_staged_emails", {})

    asyncio.run(run())
    assert kept.read_bytes() == b"keep"


def test_directory_replacement_cannot_redirect_staged_writes(stager, tmp_path, monkeypatch):
    client, root, _, _ = stager
    outside = tmp_path / "outside"
    outside.mkdir()
    save = attachment_stager._save

    def exchange(directory, entry):
        staged, = root.iterdir()
        (staged / "attachments").rename(staged / "old-attachments")
        (staged / "attachments").symlink_to(outside, target_is_directory=True)
        return save(directory, entry)

    monkeypatch.setattr(attachment_stager, "_save", exchange)

    async def run():
        async with client:
            with pytest.raises(ToolError, match="directory changed"):
                await client.call_tool("stage_email_attachments", {
                    "file_path": "/indexed/message.eml", "include_eml": False,
                })

    asyncio.run(run())
    assert list(outside.iterdir()) == []


def test_directory_replacement_cannot_redirect_clear(stager, tmp_path, monkeypatch):
    client, root, _, _ = stager
    outside = tmp_path / "outside"
    outside.mkdir()
    kept = outside / "keep"
    kept.write_bytes(b"keep")
    remove = attachment_stager._remove_contents

    async def run():
        async with client:
            reply = await client.call_tool("stage_email_attachments", {"file_path": "/indexed/message.eml"})
            staged = Path(reply.structured_content["staged_dir"])
            seen = False

            def exchange(fd):
                nonlocal seen
                if not seen:
                    seen = True
                    staged.rename(root / "displaced")
                    staged.symlink_to(outside, target_is_directory=True)
                return remove(fd)

            monkeypatch.setattr(attachment_stager, "_remove_contents", exchange)
            with pytest.raises(ToolError, match="directory changed"):
                await client.call_tool("clear_staged_emails", {"short_hash": staged.name})

    asyncio.run(run())
    assert kept.read_bytes() == b"keep"


@pytest.mark.parametrize(("headers", "chunks", "seen"), [
    ({"Content-Length": "8"}, [b"12345678"], []),
    ({"Content-Encoding": "gzip"}, [b"12345678"], []),
    ({}, [b"1234", b"5678", b"never read"], [b"1234", b"5678"]),
])
def test_response_is_bounded_before_httpx_buffers_it(monkeypatch, headers, chunks, seen):
    monkeypatch.setattr(attachment_stager, "MAX_RESPONSE_BYTES", 6)
    consumed = []
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                consumed.append(chunk)
                yield chunk

        async def aclose(self):
            closed.append(True)

    async def upstream(self, request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers=headers, stream=Stream())

    async def run():
        with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", upstream):
            async with attachment_stager._http_client(True) as client:
                with pytest.raises(ValueError, match="transfer limit|content encoding"):
                    await client.post("https://example.test/mcp")

    asyncio.run(run())
    assert consumed == seen and closed


def test_stager_http_client_does_not_follow_redirects():
    received = []

    async def upstream(self, request):
        received.append(str(request.url))
        return httpx.Response(307, headers={"Location": "http://other.test/mcp"})

    async def run():
        with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", upstream):
            async with attachment_stager._http_client(True, follow_redirects=True) as client:
                assert (await client.post("https://example.test/mcp")).status_code == 307

    asyncio.run(run())
    assert received == ["https://example.test/mcp"]
