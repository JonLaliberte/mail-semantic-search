"""Runtime resolution of the application version and build commit.

The version is derived from git tags at build time (setuptools-scm) and baked
into the package metadata, so an installed copy always knows its version. In
the Docker image we also export ``APP_VERSION``/``GIT_SHA`` build-args as env
vars, which take precedence. The chain guarantees a non-empty result even for
an unversioned local checkout.
"""

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

_DIST_NAME = "mail-semantic-search"
_DEV_FALLBACK = "0.0.0.dev0"


def resolve_version() -> str:
    """Return the running version: APP_VERSION env -> package metadata -> dev fallback."""
    env_version = (os.getenv("APP_VERSION") or "").strip()
    if env_version:
        return env_version
    try:
        return _pkg_version(_DIST_NAME)
    except PackageNotFoundError:
        return _DEV_FALLBACK


def resolve_commit() -> str:
    """Return the build commit SHA from GIT_SHA, or 'unknown' if not stamped."""
    return (os.getenv("GIT_SHA") or "").strip() or "unknown"
