"""``POST /api/mcp/try-tool``: list or run one tool of a configured MCP server, once.

Registered only while ``mcp.tool_tester`` is on, so with it off the path answers as
any unknown route does; owner-only when on. Each request starts the server as a
script cron's ``ctx.call_tool`` does (:class:`kiro_crew.cron_script.McpToolClient`),
asks ``tools/list`` and, when a tool is named, calls it once, then stops it. A tool
not declared read-only (the shareability rule: positive ``readOnlyHint`` only) needs
``confirm: true``; a switched-off tool is refused. The result is redacted as a
transcript is, then cut to :data:`_MAX_RESULT_CHARS`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from typing import Any, Callable

from aiohttp import web

logger = logging.getLogger("kiro_crew.dashboard.handlers.mcp")

_CALL_TIMEOUT_SECS = 30.0
_MAX_RESULT_CHARS = 64 * 1024
_MAX_TOOLS = 200
_MAX_LIST_CHARS = 256 * 1024
_MAX_TEXT_CHARS = 1000


def tool_tester_enabled() -> bool:
    """Whether ``mcp.tool_tester`` is on; a config that cannot be read reads as off."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load().mcp.tool_tester is True
    except Exception:
        logger.debug("mcp.tool_tester: config unreadable; the route stays off", exc_info=True)
        return False


def _redacted(text: str) -> str:
    from kiro_crew.platform.context import redact_via_context

    return redact_via_context(text)


def _redacted_schema(schema: object) -> dict:
    """*schema* redacted like the rest of a listing; ``{}`` when it is unusable or too big."""
    text = json.dumps(schema) if isinstance(schema, dict) else ""
    try:
        parsed = json.loads(_redacted(text)) if 0 < len(text) <= _MAX_RESULT_CHARS else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_rows(listed: dict, server: str) -> list[dict[str, Any]]:
    from kiro_crew.cron_script import _switched_off_tools, _tool_is_switched_off
    from kiro_crew.mcp_gateway.shareability import _all_tools_read_only

    off = _switched_off_tools(server)
    result = listed.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    rows: list[dict[str, Any]] = []
    used = 0
    for tool in (tools if isinstance(tools, list) else [])[:_MAX_TOOLS]:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name or len(name) > _MAX_TEXT_CHARS:
            continue
        text, ann = tool.get("description"), tool.get("annotations")
        schema = _redacted_schema(tool.get("inputSchema"))
        used += len(name) + len(json.dumps(schema))
        if used > _MAX_LIST_CHARS:
            break
        rows.append(
            {
                "name": name,
                "description": _redacted(text if isinstance(text, str) else "")[:_MAX_TEXT_CHARS],
                "inputSchema": schema,
                "needsConfirm": not (isinstance(ann, dict) and _all_tools_read_only([ann])),
                "switchedOff": _tool_is_switched_off(name, off),
            }
        )
    return rows


def _run_once(body: dict, on_spawn: Callable[[Any], None]) -> tuple[int, dict[str, Any]]:
    """Start the server, list its tools, call the named tool once; always stop it."""
    from kiro_crew.cron_script import McpToolClient, _resolve_mcp_server

    # The resolver caches per process; the gateway outlives an edit or a mute.
    _resolve_mcp_server.cache_clear()
    tool, refusal = body["tool"], None
    client = McpToolClient(body["server"], on_spawn=on_spawn)
    try:
        rows = _tool_rows(client.rpc("tools/list", {}), body["server"])
        row = next((r for r in rows if r["name"] == tool), None)
        if not tool:
            return 200, {"ok": True, "tools": rows}
        if row is None:
            refusal = 404, "unknown_tool"
        elif row["switchedOff"]:
            refusal = 403, "tool_switched_off"
        elif row["needsConfirm"] and body.get("confirm") is not True:
            refusal = 409, "confirm_required"
        else:
            answer = client.rpc("tools/call", {"name": tool, "arguments": body["arguments"]})
    finally:
        client.close()
    if refusal is not None:
        return refusal[0], {"error": refusal[1].replace("_", " "), "code": refusal[1]}
    result = answer.get("result", answer.get("error"))
    text = _redacted(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 200, {
        "ok": True,
        "isError": "error" in answer
        or (isinstance(result, dict) and result.get("isError") is True),
        "result": text[:_MAX_RESULT_CHARS],
        "truncated": len(text) > _MAX_RESULT_CHARS,
    }


async def api_mcp_try_tool(request: web.Request) -> web.Response:
    """POST /api/mcp/try-tool -- ``{server, tool?, arguments?, confirm?}``."""
    # Body-scope import for the cycle the sibling probe route names.
    from kiro_crew.dashboard.handlers._shared import (
        read_bounded_json,
        require_owner_dashboard_request,
    )

    denied = await require_owner_dashboard_request(request, "mcp_try_tool")
    if denied is not None:
        return denied
    body, error = await read_bounded_json(request)
    if error is not None or body is None:
        return error or web.json_response({"code": "bad_args"}, status=400)
    body.setdefault("tool", "")
    body["arguments"] = body.get("arguments") or {}
    server, tool, arguments = body.get("server"), body["tool"], body["arguments"]
    if not (isinstance(server, str) and server and isinstance(tool, str)):
        return web.json_response({"error": "name a server", "code": "bad_args"}, status=400)
    if not isinstance(arguments, dict):
        return web.json_response(
            {"error": "arguments must be an object", "code": "bad_args"}, status=400
        )
    # A server that never answers blocks the client's read; killing it ends the read.
    spawned: list[Any] = []
    stopped = threading.Event()

    def on_spawn(proc: Any) -> None:
        spawned.append(proc)
        if stopped.is_set():
            proc.kill()

    try:
        run = asyncio.to_thread(_run_once, body, on_spawn)
        status, payload = await asyncio.wait_for(run, _CALL_TIMEOUT_SECS)
    except (Exception, asyncio.CancelledError) as exc:
        stopped.set()
        for proc in spawned:
            with contextlib.suppress(OSError):
                proc.kill()
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, asyncio.TimeoutError):
            return web.json_response({"error": "no answer in time", "code": "timeout"}, status=504)
        message = _redacted(str(exc))[:_MAX_TEXT_CHARS]
        return web.json_response({"error": message, "code": "server_failed"}, status=502)
    return web.json_response(payload, status=status)
