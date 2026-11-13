"""Tests for sharing the models/index across MCP calls and releasing them when idle."""

import asyncio

import pytest

from mail_semantic_search import resources


# Stand-in for Chroma's write sequence number; tests bump it to simulate an
# index run in another process.
generation = [1]


class _FakeStore:
    opened = 0
    closed = 0

    def __init__(self):
        type(self).opened += 1

    def close(self):
        type(self).closed += 1


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    _FakeStore.opened = _FakeStore.closed = 0
    loads = {"embedding": 0, "reranker": 0}

    def _loader(kind):
        def load():
            loads[kind] += 1
            return object()

        return load

    monkeypatch.setattr(resources, "VectorStore", _FakeStore)
    monkeypatch.setattr(resources, "EmbeddingService", _loader("embedding"))
    monkeypatch.setattr(resources, "CrossEncoderReranker", _loader("reranker"))
    monkeypatch.setattr(resources, "index_generation", lambda path: generation[0])
    generation[0] = 1
    for name in ("_embedding_service", "_reranker", "_keepalive_store", "_keepalive_generation"):
        monkeypatch.setattr(resources, name, None)
    monkeypatch.setattr(resources, "_active", 0)
    return loads


def test_models_are_loaded_once_and_shared(fakes):
    assert resources.get_embedding_service() is resources.get_embedding_service()
    assert resources.get_reranker() is resources.get_reranker()
    assert fakes == {"embedding": 1, "reranker": 1}


def test_in_use_holds_one_store_open_across_calls():
    with resources.in_use():
        pass
    with resources.in_use():
        pass

    assert _FakeStore.opened == 1
    assert _FakeStore.closed == 0


def test_in_use_reopens_the_store_after_another_process_writes():
    with resources.in_use():
        pass
    generation[0] += 1
    with resources.in_use():
        pass

    assert _FakeStore.opened == 2
    assert _FakeStore.closed == 1


def test_in_use_never_reopens_mid_request():
    with resources.in_use():
        generation[0] += 1
        with resources.in_use():
            pass
        assert _FakeStore.closed == 0

    with resources.in_use():
        pass
    assert _FakeStore.opened == 2


def test_own_writes_marked_current_do_not_force_a_reload():
    """The server's scheduled index run writes into the loaded index itself."""
    with resources.in_use():
        pass
    generation[0] += 1
    resources.mark_index_current()
    with resources.in_use():
        pass

    assert _FakeStore.opened == 1
    assert _FakeStore.closed == 0


def test_mark_index_current_is_a_no_op_with_nothing_loaded():
    generation[0] += 1
    resources.mark_index_current()
    assert resources._keepalive_generation is None


def test_release_if_idle_drops_everything_and_next_use_reloads(fakes):
    with resources.in_use():
        resources.get_embedding_service()
        resources.get_reranker()

    assert resources.release_if_idle(0) is True
    assert _FakeStore.closed == 1
    assert resources.release_if_idle(0) is False  # nothing left to release

    with resources.in_use():
        resources.get_embedding_service()
    assert _FakeStore.opened == 2
    assert fakes["embedding"] == 2


def test_release_waits_for_the_idle_period():
    with resources.in_use():
        pass

    assert resources.release_if_idle(3600) is False
    assert _FakeStore.closed == 0


def test_release_never_happens_mid_request():
    with resources.in_use():
        assert resources.release_if_idle(0) is False
    assert _FakeStore.closed == 0


def test_a_scheduled_run_holds_the_index_open_and_its_writes_cause_no_reload(monkeypatch):
    monkeypatch.setattr(resources, "_indexing", 0)
    with resources.indexing():
        assert _FakeStore.opened == 1
        generation[0] += 1  # the run writes
        with resources.in_use():  # a tool call mid-run: same store, no reload
            pass
        resources.mark_index_current()
    with resources.in_use():
        pass
    assert _FakeStore.opened == 1
    assert _FakeStore.closed == 0


def test_release_waits_for_a_scheduled_index_run(fakes, monkeypatch):
    """The run holds the shared model; dropping it would make the next call load a second copy."""
    monkeypatch.setattr(resources, "_indexing", 0)
    monkeypatch.setattr(resources, "_last_used", 0.0)  # long idle
    model = resources.get_embedding_service()
    with resources.indexing():
        assert resources.release_if_idle(1) is False
        with resources.in_use():
            assert resources.get_embedding_service() is model
    assert fakes["embedding"] == 1
    assert resources.release_if_idle(0) is True


def test_in_use_survives_an_unopenable_index(monkeypatch):
    def boom():
        raise OSError("EACCES")

    monkeypatch.setattr(resources, "VectorStore", boom)

    with resources.in_use():
        pass
    assert resources._active == 0


def test_mcp_tool_calls_run_inside_in_use(monkeypatch):
    from fastmcp import Client

    from mail_semantic_search import mcp_server

    seen = []
    monkeypatch.setattr(
        mcp_server, "get_status_data_payload", lambda: seen.append(resources._active) or {}
    )

    async def call():
        async with Client(mcp_server.mcp) as client:
            await client.call_tool("get_status", {})

    asyncio.run(call())

    assert seen == [1]
    assert resources._active == 0


@pytest.mark.parametrize("raw, expected", [(None, 600.0), ("0", 0.0), ("-5", 0.0), ("90", 90.0)])
def test_idle_unload_seconds(monkeypatch, raw, expected):
    from mail_semantic_search import mcp_server

    if raw is None:
        monkeypatch.delenv("MCP_IDLE_UNLOAD_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MCP_IDLE_UNLOAD_SECONDS", raw)

    assert mcp_server._idle_unload_seconds() == expected
