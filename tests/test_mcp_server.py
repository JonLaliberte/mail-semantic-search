import subprocess
from unittest.mock import patch

import pytest

from mail_semantic_search import mcp_server


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["open", "<placeholder>"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_open_email_url_success_invokes_open_and_returns_url():
    url = "message://%3Cabc123@example.com%3E"
    with patch.object(mcp_server.subprocess, "run", return_value=_completed()) as run:
        result = mcp_server.open_email_url(url)

    run.assert_called_once_with(
        ["open", url],
        capture_output=True,
        text=True,
    )
    assert result == {"opened": url}


def test_open_email_url_raises_runtime_error_on_nonzero_exit():
    url = "message://%3Cabc123@example.com%3E"
    with patch.object(
        mcp_server.subprocess,
        "run",
        return_value=_completed(returncode=1, stderr="kLSNoLaunchPermissionErr"),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            mcp_server.open_email_url(url)

    msg = str(exc_info.value)
    assert "kLSNoLaunchPermissionErr" in msg
    assert "1" in msg
