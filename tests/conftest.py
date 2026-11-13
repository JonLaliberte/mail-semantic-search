"""Shared test setup.

CLI tests invoke `main`, which configures file logging once per process from
`config.log_path`. Without this, the suite writes its log lines (including
deliberate failures) into the real LOG_PATH from `.env`.
"""

import tempfile
from pathlib import Path

import mail_semantic_search.config as cfg_mod

cfg_mod.config.log_path = Path(tempfile.mkdtemp(prefix="mss-test-logs-")) / "test.log"
