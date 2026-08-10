"""Tests for runtime version/commit resolution.

The resolver guarantees the running app always reports *some* version, in
priority order: APP_VERSION env (set in the Docker image) -> installed package
metadata (filled by setuptools-scm from the git tag) -> a dev fallback.
"""

import importlib.metadata

from mail_semantic_search.version import resolve_commit, resolve_version


def test_resolve_version_prefers_app_version_env(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    assert resolve_version() == "1.2.3"


def test_resolve_version_falls_back_to_package_metadata(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(
        "mail_semantic_search.version._pkg_version", lambda name: "4.5.6"
    )
    assert resolve_version() == "4.5.6"


def test_resolve_version_default_when_metadata_missing(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)

    def _raise(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr("mail_semantic_search.version._pkg_version", _raise)
    assert resolve_version() == "0.0.0.dev0"


def test_resolve_version_blank_env_is_ignored(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "   ")
    monkeypatch.setattr(
        "mail_semantic_search.version._pkg_version", lambda name: "7.8.9"
    )
    assert resolve_version() == "7.8.9"


def test_resolve_commit_from_env(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc1234")
    assert resolve_commit() == "abc1234"


def test_resolve_commit_default_when_unset(monkeypatch):
    monkeypatch.delenv("GIT_SHA", raising=False)
    assert resolve_commit() == "unknown"
