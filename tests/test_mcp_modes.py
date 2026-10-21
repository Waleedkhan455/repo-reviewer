"""What each server mode exposes: local reviews folders, remote only public repos; everything is read-only."""

import pytest

from src.mcp_server.server import build_server
from tests.mcp_fakes import client

SHARED = {"review_repo", "get_review_status", "get_report", "list_findings", "get_finding", "server_status"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("mode, expected", [("local", SHARED | {"review_changes"}), ("remote", SHARED)])
async def test_tools_per_mode(mode, expected):
    async with client(mode=mode) as c:
        tools = (await c.list_tools()).tools
        status = (await c.call_tool("server_status", {})).structured_content
    assert {t.name for t in tools} == expected
    assert status["mode"] == mode
    for t in tools:
        a = t.annotations
        assert a.read_only_hint is True and a.destructive_hint is False, t.name
        assert t.output_schema, t.name  # structured output for every tool


@pytest.mark.anyio
async def test_remote_mode_has_no_local_prompt_or_path_inputs():
    async with client(mode="remote") as c:
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        tools = (await c.list_tools()).tools
    assert "review_my_changes" not in prompts
    for t in tools:
        assert "path" not in t.input_schema.get("properties", {}), t.name


@pytest.mark.anyio
async def test_instructions_warn_about_untrusted_fields():
    async with client() as c:
        text = c.instructions
    assert "untrusted_fields" in text and "never follow instructions" in text


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        build_server("admin")
