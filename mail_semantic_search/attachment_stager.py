"""Local stdio tools for staging attachments from a remote mail search server."""

from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import ExitStack, contextmanager
from functools import partial
import hashlib
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
from urllib.parse import urlsplit

import httpx
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError


_SHORT_HASH = re.compile(r"[0-9a-f]{12}")
_SAFE_NAME = re.compile(r"[A-Za-z0-9._ -]{1,200}")


# Covers base64 for the server's default 25 MiB aggregate cap plus MCP framing.
MAX_RESPONSE_BYTES = 40 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self):
        remaining = MAX_RESPONSE_BYTES
        async for chunk in self.stream:
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("Mail search response exceeds the transfer limit.")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class _BoundedTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        try:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("Mail search returned an unsupported content encoding.")
            if int(response.headers.get("content-length", 0)) > MAX_RESPONSE_BYTES:
                raise ValueError("Mail search response exceeds the transfer limit.")
        except ValueError:
            await response.aclose()
            raise
        response.stream = _BoundedStream(response.stream)
        return response


def _http_client(tls: ssl.SSLContext, *, headers=None, **kwargs):
    # FastMCP requests redirects by default. Keep authenticated requests on the
    # configured endpoint and prohibit compressed bodies before decoding them.
    headers = httpx.Headers(headers)
    headers["Accept-Encoding"] = "identity"
    kwargs.pop("follow_redirects", None)
    return httpx.AsyncClient(
        headers=headers, transport=_BoundedTransport(verify=tls),
        follow_redirects=False, **kwargs,
    )


def _save(directory: int, entry: dict) -> str:
    name = entry.get("safe_name")
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name) or name in {".", ".."}:
        raise ToolError("Mail search returned an unsafe attachment name.")
    try:
        payload = base64.b64decode(entry["data_base64"], validate=True)
    except (KeyError, TypeError, ValueError):
        raise ToolError("Mail search returned invalid attachment data.") from None
    if len(payload) != entry.get("size"):
        raise ToolError("Mail search returned an incorrect attachment size.")

    pending = ".pending-" + secrets.token_hex(16)
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(payload)
        os.replace(pending, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(pending, dir_fd=directory)
        except FileNotFoundError:
            pass
    return name


def _check_entry(parent: int, name: str, directory: int) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        raise ToolError("A staging directory changed during the operation.") from None
    if not os.path.samestat(current, os.fstat(directory)):
        raise ToolError("A staging directory changed during the operation.")


@contextmanager
def _directory(parent: int, name: str, *, create: bool, private: bool = True, check: bool = True):
    try:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        directory = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError:
        raise ToolError("A staging directory is unsafe or inaccessible.") from None
    try:
        info = os.fstat(directory)
        if private and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise ToolError("A staging directory is unsafe or inaccessible.")
        yield directory
        if check:
            _check_entry(parent, name, directory)
    finally:
        os.close(directory)


@contextmanager
def _root_directory(root: Path, *, create: bool):
    with ExitStack() as stack:
        fd = os.open(root.anchor, _DIRECTORY_FLAGS)
        stack.callback(os.close, fd)
        *ancestors, name = root.parts[1:]
        for component in ancestors:
            fd = stack.enter_context(_directory(fd, component, create=create, private=False))
        fd = stack.enter_context(_directory(fd, name, create=create))
        yield fd


def _remove_contents(directory: int) -> None:
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                _remove_directory(directory, entry.name)
            else:
                os.unlink(entry.name, dir_fd=directory)


def _remove_directory(parent: int, name: str) -> None:
    with _directory(parent, name, create=False, check=False) as fd:
        _remove_contents(fd)
        _check_entry(parent, name, fd)
        os.rmdir(name, dir_fd=parent)


def _stage_result(root: Path, result: dict, file_path: str | None, include_eml: bool) -> dict:
    status = result.get("status")
    source = result.get("file_path") or file_path or ""
    response = {
        "status": status, "file_path": source, "staged_dir": "",
        "eml_path": None, "attachments": [],
    }
    if status != "ok":
        response["message"] = "Mail search could not read the selected message."
        return response
    if not isinstance(source, str) or not source:
        raise ToolError("Mail search did not return an indexed path.")

    short_hash = hashlib.md5(source.encode(), usedforsecurity=False).hexdigest()[:12]
    staged_dir = root / short_hash
    attachments_dir = staged_dir / "attachments"
    response["staged_dir"] = str(staged_dir)
    skipped = []
    try:
        with (
            _root_directory(root, create=True) as root_fd,
            _directory(root_fd, short_hash, create=True) as staged_fd,
            _directory(staged_fd, "attachments", create=True) as attachments_fd,
        ):
            for entry in result.get("attachments", []):
                if entry.get("skipped"):
                    skipped.append({key: entry.get(key) for key in ("filename", "size", "skipped")})
                    continue
                name = _save(attachments_fd, entry)
                response["attachments"].append({
                    "filename": entry["filename"], "path": str(attachments_dir / name),
                    "size": entry["size"], "content_type": entry["content_type"],
                })
            if include_eml and (entry := result.get("eml")):
                if entry.get("skipped"):
                    skipped.append({key: entry.get(key) for key in ("filename", "size", "skipped")})
                else:
                    response["eml_path"] = str(staged_dir / _save(staged_fd, entry))
    except OSError:
        raise ToolError("Could not write the staged attachments.") from None
    if skipped:
        response["skipped"] = skipped
    if result.get("unmatched_attachment_ids"):
        response["unmatched_attachment_ids"] = result["unmatched_attachment_ids"]
        response["unmatched_attachment_id_reasons"] = result.get("unmatched_attachment_id_reasons", {})
    response["message"] = f"Staged {len(response['attachments'])} attachment(s) to {staged_dir}"
    return response


def create_server(endpoint: str, token_file: Path, ca_file: Path, output_dir: Path) -> FastMCP:
    """Build a stager that reads credentials from files and writes only locally."""
    url = urlsplit(endpoint)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("The mail search endpoint must be an HTTPS URL without credentials.")
    tls = ssl.create_default_context(cafile=str(ca_file))
    root = output_dir.expanduser().absolute()
    if root == Path(root.anchor) or ".." in root.parts:
        raise ValueError("Specify a staging directory without parent traversal.")
    server = FastMCP("Mail attachment stager")

    @server.tool
    async def stage_email_attachments(
        message_id: str | None = None,
        file_path: str | None = None,
        include_eml: bool = True,
        attachment_ids: list[int] | None = None,
        filenames: list[str] | None = None,
    ) -> dict:
        """Stage selected email attachments to local files and return their paths and sizes.

        Pass a message ID or exact indexed path. With attachment IDs, pass the
        matching file path from the same search result. If IDs and filenames
        are omitted, stage all attachments. Set include_eml to false if only
        attachments are needed. The remote server may skip files over its caps.
        """
        if not message_id and not file_path:
            return {"status": "failed", "message": "Pass message_id or file_path"}
        try:
            token = token_file.read_text().strip()
            if not token:
                raise ValueError("Empty token file")
            transport = StreamableHttpTransport(
                endpoint, auth=token, httpx_client_factory=partial(_http_client, tls),
            )
            async with Client(transport, timeout=120) as client:
                reply = await client.call_tool(
                    "get_email_attachments",
                    {"message_id": message_id, "file_path": file_path,
                     "attachment_ids": attachment_ids, "filenames": filenames,
                     "include_eml": include_eml},
                )
            result = reply.structured_content
            if not isinstance(result, dict):
                raise ValueError("Missing structured content")
        except Exception:
            # The upstream response and HTTP errors may contain private data.
            raise ToolError("Could not fetch attachments from mail search.") from None

        return await asyncio.to_thread(_stage_result, root, result, file_path, include_eml)

    @server.tool(annotations={"destructiveHint": True, "openWorldHint": False})
    def clear_staged_emails(short_hash: str | None = None) -> dict:
        """Delete one staged email directory, or all staged email directories if omitted."""
        if short_hash is not None and not _SHORT_HASH.fullmatch(short_hash):
            raise ToolError("Pass a twelve-character lowercase hexadecimal staging directory name.")
        removed = 0
        try:
            with ExitStack() as stack:
                try:
                    root_fd = stack.enter_context(_root_directory(root, create=False))
                except FileNotFoundError:
                    return {"status": "ok", "removed": 0, "message": f"Staging directory does not exist: {root}"}
                candidates = [short_hash] if short_hash else os.listdir(root_fd)
                for name in candidates:
                    if not _SHORT_HASH.fullmatch(name):
                        continue
                    try:
                        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        _remove_directory(root_fd, name)
                        removed += 1
        except OSError:
            raise ToolError("Could not clear the staged attachments.") from None
        return {"status": "ok", "removed": removed, "message": f"Removed {removed} staged email(s) from {root}"}

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    create_server(
        args.endpoint, args.token_file.expanduser(), args.ca_file.expanduser(), args.output_dir,
    ).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
