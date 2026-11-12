"""An app does not make a scheduled job or a background run a Bot's by naming the Bot.

The App Kit SDKs already hold this for their own creates (``apps.cron_sdk``,
``apps.spawn_sdk``). The HTTP routes an app token can reach, and the ``cron_add``
tool an agent in an app's session can call, take a Bot's name directly:
``POST /api/crons`` (``agent`` or ``member_id``), ``POST /api/crons/tools`` and
``POST /api/spawn`` (``crew`` / ``target_member``, or a parent session that is a
Bot's). Each refuses a caller with an app claim when the job or run would be a Bot's
own -- its memory store or its chat -- stores nothing and starts nothing. The owner
still schedules and delegates to a Bot, an app still names an agent of its own, and a
``config.agents`` row on the shared store is still an agent's name for an app.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from handlers_messaging_coverage_helpers import _Req, _state

import kiro_crew.dashboard.handlers.messaging as messaging
from kiro_crew import mcp_cron, member_memory_auth
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.cron import CronService
from kiro_crew.dashboard import session_control, slot_ownership, token_auth
from kiro_crew.dashboard.handlers import cron as cron_handlers
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    bind_session_execution,
    resolve_member_execution,
)
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT
from kiro_crew.memory_stores import persist_member_config, provision_member_memory

APP = "shop"
OWNER_SUBJECT = "local-app"
#: A Bot with private memory, and a ``config.agents`` row on the shared store.
BOT = "reviewer"
SHARED_ROW = "plain"
#: A session that already runs as the Bot.
BOT_SESSION = "dashboard:bot-chat"
#: One of the app's own agents, which an app token's run names.
OWN_AGENT = "shop--helper"


@pytest.fixture
def bot() -> SimpleNamespace:
    """The Bot's id and store, with the shared-store row beside it."""
    cfg = KiroCrewConfig.load()
    cfg.agents[BOT] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, BOT)
    persist_member_config(cfg, BOT, create=True)
    cfg = KiroCrewConfig.load()
    cfg.agents[SHARED_ROW] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()
    cfg = KiroCrewConfig.load()
    execution = resolve_member_execution(cfg, BOT)
    bind_session_execution(BOT_SESSION, execution)
    return SimpleNamespace(member_id=cfg.agents[BOT].member_id, store=store, execution=execution)


@pytest.fixture
def sel_calls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import kiro_crew.sel as sel_mod

    recorder = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    # The app-isolation rows go through slot_ownership, which binds sel at import.
    monkeypatch.setattr(slot_ownership, "sel", lambda: recorder)
    return recorder


def _denials(sel_calls: MagicMock) -> list[tuple[str, str]]:
    return [
        (call.kwargs["caller"], call.kwargs["operation"])
        for call in sel_calls.log_api_access.call_args_list
        if call.kwargs.get("outcome") == "denied"
    ]


# ── POST /api/crons ──


@pytest.fixture
def svc(tmp_path) -> CronService:
    return CronService(base_dir=tmp_path / "crons")


async def _create_cron(svc: CronService, app_claim: str, **fields) -> tuple[int, dict]:
    @web.middleware
    async def identity(request: web.Request, handler):
        request["user"] = OWNER_SUBJECT
        request["app"] = app_claim
        if app_claim:
            denied = token_auth._enforce_app_scope(request, app_claim, request.path)
            if denied is not None:
                return denied
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(
        crons=svc, owner_id="", push_refresh=MagicMock(), has_slot=MagicMock(return_value=False)
    )
    app.router.add_post("/api/crons", cron_handlers.api_crons_create)
    async with TestClient(TestServer(app)) as client:
        # flake-ok: the in-process test server answers on its own; pytest-timeout bounds the test
        resp = await client.post(
            "/api/crons", json={"name": "job", "message": "task", "every": 3600, **fields}
        )
        return resp.status, await resp.json()


@pytest.fixture
def grant_crons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        token_auth, "_app_api_allowlist", lambda name: ("/api/crons",) if name == APP else ()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["agent", "member_id"])
async def test_an_app_token_cannot_schedule_a_job_as_a_bot(
    svc, bot, grant_crons, sel_calls, field
) -> None:
    named = {"agent": BOT} if field == "agent" else {"member_id": bot.member_id}
    status, payload = await _create_cron(svc, APP, **named)

    assert status == 403, payload
    assert payload["code"] == "owner_only"
    assert svc.list_jobs() == []
    assert _denials(sel_calls) == [(f"app:{APP}", "crons.create")]


@pytest.mark.asyncio
async def test_the_owner_still_schedules_a_job_as_a_bot(svc, bot, sel_calls) -> None:
    status, payload = await _create_cron(svc, "", agent=BOT)

    assert status == 200, payload
    (job,) = svc.list_jobs()
    assert job.member_id == bot.member_id
    assert job.execution_context["store"]["store_id"] == bot.store
    assert _denials(sel_calls) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["shop--helper", SHARED_ROW, ""])
async def test_an_app_token_still_schedules_its_own_job(
    svc, bot, grant_crons, sel_calls, agent
) -> None:
    """An agent of the app's own, a shared-store row, or no agent at all."""
    status, payload = await _create_cron(svc, APP, **({"agent": agent} if agent else {}))

    assert status == 200, payload
    (job,) = svc.list_jobs()
    assert (job.created_by, job.agent_id, job.member_id) == (f"app:{APP}", agent, "")
    assert job.execution_context["store"]["store_id"] == "default"
    assert job.execution_context["member_id"] is None
    assert _denials(sel_calls) == []


# ── POST /api/crons/tools ──


@pytest.fixture
def tool_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def _local(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        return "ok"

    async def _store(request: web.Request, state: object, operation: str):
        return "", None

    monkeypatch.setattr(mcp_cron, "_call_tool_locally", _local)
    monkeypatch.setattr(cron_handlers, "resolve_lesson_memory_store", _store)
    # Whose tab the caller's session is does not bear on what it may name.
    monkeypatch.setattr(session_control, "is_person_tab", lambda state, session_key: False)
    monkeypatch.setattr(
        member_memory_auth,
        "memory_request_identity",
        lambda request: (request.headers.get("X-Session-Key", ""), True),
    )
    return calls


async def _cron_add(app_claim: str, session: str, arguments: dict) -> tuple[int, dict]:
    @web.middleware
    async def internal(request: web.Request, handler):
        request["internal_auth"] = True
        if app_claim:
            request["app"] = app_claim
        return await handler(request)

    app = web.Application(middlewares=[internal])
    app["state"] = SimpleNamespace(push_refresh=MagicMock(), _slots={})
    app.router.add_post("/api/crons/tools", cron_handlers.api_cron_tools)
    async with TestClient(TestServer(app)) as client:
        # flake-ok: the in-process test server answers on its own; pytest-timeout bounds the test
        resp = await client.post(
            "/api/crons/tools",
            json={"name": "cron_add", "arguments": arguments},
            headers={"X-Session-Key": session},
        )
        return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["agent", "member_id"])
async def test_an_agent_in_an_apps_session_cannot_schedule_a_job_as_a_bot(
    bot, tool_calls, sel_calls, field
) -> None:
    named = {"agent": BOT} if field == "agent" else {"member_id": bot.member_id}
    status, payload = await _cron_add(APP, "dashboard:shop-1", {"name": "job", **named})

    assert status == 200, payload
    assert payload["result"].startswith("Error: an app's schedule cannot run as a Bot")
    assert tool_calls == []
    assert _denials(sel_calls) == [(f"app:{APP}", "cron.tools")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_claim", "session", "agent"),
    [
        # A person's own session names the Bot.
        ("", "dashboard:mine", BOT),
        # A session that already runs as the Bot schedules as itself.
        (APP, BOT_SESSION, BOT),
        # An app's session names an agent of its own, or a shared-store row.
        (APP, "dashboard:shop-1", "shop--helper"),
        (APP, "dashboard:shop-1", SHARED_ROW),
    ],
)
async def test_the_cron_tool_still_runs_for_every_other_caller(
    bot, tool_calls, sel_calls, app_claim, session, agent
) -> None:
    arguments = {"name": "job", "agent": agent}
    status, payload = await _cron_add(app_claim, session, arguments)

    assert (status, payload) == (200, {"result": "ok"})
    assert tool_calls == [("cron_add", arguments)]
    assert _denials(sel_calls) == []


# ── POST /api/spawn ──


def _spawn(body: dict, app_claim: str) -> tuple[web.Response, MagicMock]:
    """Drive ``api_spawn`` with *body* as *app_claim* (``""`` is the owner)."""
    manager = MagicMock(max_concurrent=4, all_agents=[], _agents={}, _tasks={})
    manager.get.return_value = None
    manager.spawn.return_value = SimpleNamespace(
        id="a1", done=False, error="", error_code="", agent="kirocrew", memory_store="", crew=""
    )
    request = _Req(_state(subagents=manager), body, extra={"app": app_claim})
    return asyncio.run(messaging.api_spawn(request)), manager


@pytest.mark.parametrize("field", ["crew", "target_member"])
def test_an_app_token_cannot_start_a_run_as_a_bot(bot, sel_calls, field) -> None:
    response, manager = _spawn({"task": "summarise", "agent": OWN_AGENT, field: BOT}, APP)

    assert response.status == 404
    # The body a member that does not exist gets.
    assert json.loads(response.text) == {
        "error": "The target member does not exist.",
        "code": "unknown_member",
    }
    manager.spawn.assert_not_called()
    assert _denials(sel_calls) == [(APP, "spawn.create")]


def test_an_app_token_cannot_start_a_run_under_a_bots_session(bot, sel_calls) -> None:
    body = {"task": "summarise", "agent": OWN_AGENT, "parent_session": BOT_SESSION}
    response, manager = _spawn(body, APP)

    assert response.status == 409
    assert json.loads(response.text)["code"] == "member_identity_unavailable"
    manager.spawn.assert_not_called()
    assert _denials(sel_calls) == [(APP, "spawn.create")]


def test_the_owner_still_delegates_a_run_to_a_bot(bot, sel_calls) -> None:
    response, manager = _spawn({"task": "summarise", "crew": BOT}, "")

    assert response.status == 200, response.text
    assert manager.spawn.call_args.kwargs["memory_store"] == bot.store
    assert _denials(sel_calls) == []


@pytest.mark.parametrize("body", [{}, {"crew": SHARED_ROW}])
def test_an_app_token_still_starts_a_run_of_its_own(bot, sel_calls, body) -> None:
    response, manager = _spawn({"task": "summarise", "agent": OWN_AGENT, **body}, APP)

    assert response.status == 200, response.text
    assert manager.spawn.call_args.kwargs["memory_store"] == ""
    assert _denials(sel_calls) == []


def test_a_caller_that_already_runs_as_the_bot_hands_work_to_itself(bot) -> None:
    request = _Req(None, {}, extra={"app": APP})

    refused = messaging._app_bot_spawn_refusal(request, bot.execution, bot.execution, crew=BOT)

    assert refused is None


# ── what counts as a Bot's own ──


def test_a_bots_own_is_its_memory_store_or_its_chat() -> None:
    from kiro_crew.execution_context import is_bot_own_execution

    def execution(kind: str, *, store: str = "default", member_id=None, template="kirocrew"):
        return ExecutionContext(member_id, MemoryStoreRef(store, member_id), kind, template)

    assert is_bot_own_execution(execution("template", store="reviewer-store", member_id="m-1"))
    assert is_bot_own_execution(execution("member", store="reviewer-store"))
    assert is_bot_own_execution(execution("member", template=MATE_CONDUCTOR_AGENT))
    assert not is_bot_own_execution(execution("member"))
    assert not is_bot_own_execution(execution("template"))
    assert not is_bot_own_execution(execution("template", template=MATE_CONDUCTOR_AGENT))
    assert not is_bot_own_execution(None)
