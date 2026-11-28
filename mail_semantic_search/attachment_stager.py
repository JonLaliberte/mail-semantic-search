"""Local stdio tools for staging attachments from a remote mail search server."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
from pathlib import Path
import re
import shutil
import ssl
import tempfile
from urllib.parse import urlsplit

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError


_SHORT_HASH = re.compile(r"[0-9a-f]{12}")
_SAFE_NAME = re.compile(r"[A-Za-z0-9._ -]{1,200}")


def _save(directory: Path, entry: dict) -> Path:
    name = entry.get("safe_name")
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name) or name in {".", ".."}:
        raise ToolError("Mail search returned an unsafe attachment name.")
    try:
        payload = base64.b64decode(entry["data_base64"], validate=True)
    except (KeyError, TypeError, ValueError):
        raise ToolError("Mail search returned invalid attachment data.") from None
    if len(payload) != entry.get("size"):
        raise ToolError("Mail search returned an incorrect attachment size.")

    target = directory / name
    # Replace the directory entry atomically, including when it is a symlink
    # or a hard link left by a local reader. Never follow it to write the file.
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
        pending = Path(temporary.name)
        try:
            temporary.write(payload)
            temporary.close()
            pending.replace(target)
        finally:
            pending.unlink(missing_ok=True)
    return target


def _directory(parent: Path, name: str) -> Path:
    path = parent / name
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ToolError("A staging subdirectory is unsafe.")
    return path


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

    root.mkdir(mode=0o700, exist_ok=True, parents=True)
    if root.is_symlink() or not root.is_dir():
        raise ToolError("The staging root is unsafe.")
    short_hash = hashlib.md5(source.encode(), usedforsecurity=False).hexdigest()[:12]
    staged_dir = _directory(root, short_hash)
    attachments_dir = _directory(staged_dir, "attachments")
    response["staged_dir"] = str(staged_dir)
    skipped = []
    for entry in result.get("attachments", []):
        if entry.get("skipped"):
            skipped.append({key: entry.get(key) for key in ("filename", "size", "skipped")})
            continue
        path = _save(attachments_dir, entry)
        response["attachments"].append({
            "filename": entry["filename"], "path": str(path),
            "size": entry["size"], "content_type": entry["content_type"],
        })
    if include_eml and (entry := result.get("eml")):
        if entry.get("skipped"):
            skipped.append({key: entry.get(key) for key in ("filename", "size", "skipped")})
        else:
            response["eml_path"] = str(_save(staged_dir, entry))
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
    root = output_dir.expanduser().resolve()
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
            async with Client(endpoint, auth=token, verify=tls, timeout=120) as client:
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
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ToolError("The staging root is unsafe.")
        candidates = [root / short_hash] if short_hash else (root.iterdir() if root.exists() else ())
        removed = 0
        for directory in candidates:
            if _SHORT_HASH.fullmatch(directory.name) and not directory.is_symlink() and directory.is_dir():
                shutil.rmtree(directory)
                removed += 1
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
