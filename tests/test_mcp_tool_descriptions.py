"""MCP tools should publish enough schema guidance for agents to use them safely."""

import asyncio
import json
import os
import subprocess
import sys

from mail_semantic_search.mcp_server import mcp


def _tools_by_name():
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def test_tool_parameters_have_schema_descriptions():
    tools = _tools_by_name()
    core_names = {
        "search_emails",
        "query_emails",
        "get_status",
        "list_inbox_emails",
        "get_email_attachments",
        "stage_email_attachments",
        "clear_staged_emails",
    }

    assert core_names <= tools.keys()
    for name, tool in tools.items():
        for parameter, schema in tool.parameters.get("properties", {}).items():
            assert schema.get("description"), f"{name}.{parameter} needs a description"


def test_search_tools_explain_selection_and_or_workflow():
    tools = _tools_by_name()
    semantic_description = " ".join(tools["search_emails"].description.split())
    metadata_description = " ".join(tools["query_emails"].description.split())

    assert "query_emails" in semantic_description
    assert "semantic" in semantic_description.lower()
    assert "sent to OR from" in semantic_description
    assert "search_emails" in metadata_description
    assert "metadata" in metadata_description.lower()
    assert "sent to OR from" in metadata_description


def test_state_changing_tools_are_labeled_when_available():
    tools = _tools_by_name()
    for name in ("mark_email_read", "archive_email", "mark_read_and_archive"):
        if name in tools:  # These tools are registered only on macOS.
            assert "state-chang" in tools[name].description.lower()

    assert "never source emails" in tools["clear_staged_emails"].description


def test_disable_staging_tools_omits_only_staging_tools():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, json; from mail_semantic_search.mcp_server import mcp; "
            "print(json.dumps(sorted(tool.name for tool in asyncio.run(mcp.list_tools()))))",
        ],
        env={**os.environ, "DISABLE_STAGING_TOOLS": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert set(json.loads(result.stdout)) == _tools_by_name().keys() - {
        "stage_email_attachments",
        "clear_staged_emails",
    }
