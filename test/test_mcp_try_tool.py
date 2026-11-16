"""``POST /api/mcp/try-tool``, the MCP Servers page's Try a tool panel.

Off (the default) the path is not registered, so it answers as any unknown route.
On it is owner-only, a tool not declared read-only needs a confirm, a run is
stopped at the time limit, and the result is redacted and then cut to size.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.routes import agent_config as agent_config_routes
from kiro_crew.testing.synthetic_credentials import github_classic

pytestmark = pytest.mark.asyncio

_PATH = "/api/mcp/try-tool"


class _FakeState:
    owner_id = ""


def _build_app() -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        # Stand-in for token_auth_middleware, as in the owner-gate invariant test.
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    agent_config_routes.register(app)
    return app


class _FakeProc:
    def __init__(self) -> None:
        self.killed = threading.Event()

    def kill(self) -> None:
        self.killed.set()


class _FakeClient:
    """Stands in for ``McpToolClient``: answers ``tools/list`` and ``tools/call``."""

    tools: list[dict[str, Any]] = []
    answer: dict[str, Any] = {}
    block_call = False
    on_call = staticmethod(lambda: None)
    instances: list["_FakeClient"] = []

    def __init__(self, server: str, session_key: str = "", *, on_spawn=None) -> None:
        self.server = server
        self.proc = _FakeProc()
        self.sent: list[tuple[str, dict]] = []
        self.closed = False
        type(self).instances.append(self)
        if on_spawn is not None:
            on_spawn(self.proc)

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params or {}))
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": 2, "result": {"tools": type(self).tools}}
        if type(self).block_call:
            type(self).on_call()
            # Unblocked only by the route stopping the server, as a real kill ends a read.
            assert self.proc.killed.wait(timeout=30)
            raise RuntimeError("MCP server 'fake' disconnected during 'tools/call'")
        return {"jsonrpc": "2.0", "id": 3, "result": type(self).answer}

    def close(self) -> None:
        self.closed = True


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


@pytest.fixture
def tester(_floor_monkeypatch):
    """The route switched on, its client replaced, and no tool switched off."""
    from kiro_crew import cron_script

    _floor_monkeypatch.setattr(agent_config_routes, "tool_tester_enabled", lambda: True)
    _floor_monkeypatch.setattr(cron_script, "McpToolClient", _FakeClient)
    _floor_monkeypatch.setattr(cron_script, "_switched_off_tools", lambda server: frozenset())
    _floor_monkeypatch.setattr(_FakeClient, "tools", [])
    _floor_monkeypatch.setattr(_FakeClient, "answer", _text("done"))
    _floor_monkeypatch.setattr(_FakeClient, "block_call", False)
    _floor_monkeypatch.setattr(_FakeClient, "on_call", staticmethod(lambda: None))
    _floor_monkeypatch.setattr(_FakeClient, "instances", [])
    return _FakeClient


async def _post(body: object, **headers: str) -> tuple[int, Any]:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.post(_PATH, json=body, headers=headers)
        text = await resp.text()
        return resp.status, (json.loads(text) if text.startswith("{") else text)


async def test_off_the_path_answers_as_an_unknown_route() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        unknown = await client.post("/api/mcp/no-such-route", json={"server": "x"})
        tester = await client.post(_PATH, json={"server": "x"})
        assert (tester.status, await tester.text()) == (unknown.status, await unknown.text())
        assert tester.status == 404


async def test_on_a_non_owner_is_refused_before_anything_starts(tester) -> None:
    status, body = await _post({"server": "s", "tool": "t"}, **{"X-Test-User": "someone-else"})
    assert status in (401, 403)
    assert body["code"] == "owner_only"
    assert tester.instances == []


async def test_without_a_tool_it_lists_tools_with_their_confirm_need(tester) -> None:
    tester.tools = [
        {"name": "read", "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object"}},
        {"name": "write", "annotations": {"readOnlyHint": False, "destructiveHint": False}},
        {"name": "bare", "description": "no annotations"},
    ]
    status, body = await _post({"server": "s"})
    assert status == 200
    assert [(r["name"], r["needsConfirm"]) for r in body["tools"]] == [
        ("read", False),
        ("write", True),
        ("bare", True),
    ]
    assert body["tools"][0]["inputSchema"] == {"type": "object"}
    assert [m for m, _ in tester.instances[0].sent] == ["tools/list"]
    assert tester.instances[0].closed


@pytest.mark.parametrize(
    "annotations",
    [None, {}, {"readOnlyHint": False}, {"destructiveHint": True}, {"readOnlyHint": "true"}],
)
async def test_a_tool_not_declared_read_only_is_refused_without_the_confirm(
    tester, annotations
) -> None:
    tool: dict[str, Any] = {"name": "t"}
    if annotations is not None:
        tool["annotations"] = annotations
    tester.tools = [tool]
    status, body = await _post({"server": "s", "tool": "t", "arguments": {"a": 1}})
    assert status == 409
    assert body["code"] == "confirm_required"
    assert [m for m, _ in tester.instances[0].sent] == ["tools/list"]
    assert tester.instances[0].closed

    status, body = await _post({"server": "s", "tool": "t", "arguments": {"a": 1}, "confirm": True})
    assert status == 200
    assert tester.instances[1].sent[-1] == ("tools/call", {"name": "t", "arguments": {"a": 1}})


async def test_a_read_only_tool_runs_without_a_confirm(tester) -> None:
    tester.tools = [{"name": "t", "annotations": {"readOnlyHint": True}}]
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200
    assert body["isError"] is False
    assert json.loads(body["result"]) == _text("done")


async def test_a_switched_off_tool_is_refused(tester, _floor_monkeypatch) -> None:
    from kiro_crew import cron_script

    _floor_monkeypatch.setattr(cron_script, "_switched_off_tools", lambda s: frozenset({"t"}))
    tester.tools = [{"name": "t", "annotations": {"readOnlyHint": True}}]
    status, body = await _post({"server": "s", "tool": "t", "confirm": True})
    assert status == 403
    assert body["code"] == "tool_switched_off"
    assert [m for m, _ in tester.instances[0].sent] == ["tools/list"]


async def test_a_run_past_the_time_limit_stops_the_server(tester, _floor_monkeypatch) -> None:
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    # The fake call answers only once the route kills it, so any limit expires.
    _floor_monkeypatch.setattr(module, "_CALL_TIMEOUT_SECS", 0.01)  # flake-ok: never answered
    tester.tools = [{"name": "t", "annotations": {"readOnlyHint": True}}]
    tester.block_call = True
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 504
    assert body["code"] == "timeout"
    assert tester.instances[0].proc.killed.wait(timeout=30)


async def test_a_large_result_is_cut_with_a_note(tester) -> None:
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    tester.tools = [{"name": "t", "annotations": {"readOnlyHint": True}}]
    tester.answer = _text("x" * (module._MAX_RESULT_CHARS * 2))
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200
    assert len(body["result"]) == module._MAX_RESULT_CHARS
    assert body["truncated"] is True


async def test_a_credential_in_the_result_is_redacted(tester) -> None:
    token = github_classic()
    tester.tools = [{"name": "t", "annotations": {"readOnlyHint": True}, "description": token}]
    tester.answer = _text(f"token={token}")
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200
    assert token not in body["result"]
    status, body = await _post({"server": "s"})
    assert token not in body["tools"][0]["description"]


async def test_a_server_that_fails_to_start_reports_redacted(tester, _floor_monkeypatch) -> None:
    token = github_classic(seed=7)

    def refuse(*args, **kwargs):
        raise RuntimeError(f"MCP server 's' disconnected; stderr tail: {token}")

    _floor_monkeypatch.setattr(tester, "__init__", refuse)
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 502
    assert body["code"] == "server_failed"
    assert token not in body["error"]


async def test_a_cancelled_request_stops_the_server(tester) -> None:
    tester.tools = [{"name": "t", "annotations": {"readOnlyHint": True}}]
    tester.block_call = True
    async with TestClient(TestServer(_build_app())) as client:
        started, loop = asyncio.Event(), asyncio.get_running_loop()
        tester.on_call = staticmethod(lambda: loop.call_soon_threadsafe(started.set))
        pending = asyncio.ensure_future(client.post(_PATH, json={"server": "s", "tool": "t"}))
        await asyncio.wait_for(started.wait(), 30)
        # A gateway shutdown cancels each connection's task, and the handler with it.
        for task in asyncio.all_tasks():
            if "RequestHandler.start" in getattr(task.get_coro(), "__qualname__", ""):
                task.cancel()
        # The client's read ends once the server drops the connection, after the kill.
        with pytest.raises(Exception):
            await pending
        assert tester.instances[0].proc.killed.is_set()


async def test_a_listing_is_redacted_and_bounded(tester) -> None:
    secret = github_classic(seed=3)
    schema = {"type": "object", "properties": {"k": {"type": "string", "default": secret}}}
    tester.tools = [{"name": "t", "inputSchema": schema}, {"name": "n" * 5000}]
    tester.tools += [
        {"name": f"big{i}", "inputSchema": {"description": "x" * 40000}} for i in range(100)
    ]
    status, body = await _post({"server": "s"})
    assert status == 200
    assert secret not in json.dumps(body)
    assert body["tools"][0]["inputSchema"]["properties"]["k"]["type"] == "string"
    assert all(len(row["name"]) <= 1000 for row in body["tools"])
    assert len(json.dumps(body)) < 512 * 1024


async def test_a_server_edited_on_the_page_is_read_afresh(tester, _floor_monkeypatch) -> None:
    import functools

    from kiro_crew import cron_script

    @functools.lru_cache(maxsize=16)
    def resolve(name: str) -> tuple:
        return ((name,), {})

    resolve("s")
    _floor_monkeypatch.setattr(cron_script, "_resolve_mcp_server", resolve)
    tester.tools = [{"name": "t", "annotations": {"readOnlyHint": True}}]
    status, _ = await _post({"server": "s", "tool": "t"})
    assert status == 200
    assert resolve.cache_info().currsize == 0


@pytest.mark.parametrize("body", [{}, {"server": ""}, {"server": "s", "arguments": [1]}])
async def test_a_malformed_request_is_refused(tester, body) -> None:
    status, answer = await _post(body)
    assert status == 400
    assert answer["code"] == "bad_args"
    assert tester.instances == []


async def test_the_client_hands_over_its_process_before_initialize(_floor_monkeypatch) -> None:
    from unittest.mock import MagicMock

    from kiro_crew import cron_script

    proc = MagicMock()
    proc.stdout.readline.return_value = '{"jsonrpc":"2.0","id":1,"result":{}}\n'
    seen: list[tuple[object, int]] = []
    _floor_monkeypatch.setattr(cron_script, "_resolve_mcp_server", lambda name: (("srv",), {}))
    _floor_monkeypatch.setattr(cron_script, "wrap_argv", lambda argv, mode: (argv, None))
    _floor_monkeypatch.setattr(cron_script, "cgroup_scope_argv", lambda argv: argv)
    _floor_monkeypatch.setattr(cron_script, "popen_limited", lambda *a, **k: proc)
    client = cron_script.McpToolClient(
        "srv", on_spawn=lambda p: seen.append((p, proc.stdin.write.call_count))
    )
    assert seen == [(proc, 0)]
    proc.stdout.readline.return_value = '{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n'
    assert client.rpc("tools/list") == {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
    client.close()


async def test_the_setting_is_off_unless_it_is_a_real_true() -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.section_builders import _build_mcp_config

    assert KiroCrewConfig().mcp.tool_tester is False
    assert _build_mcp_config({"tool_tester": "true"}).tool_tester is False
    assert _build_mcp_config({"tool_tester": True}).tool_tester is True
