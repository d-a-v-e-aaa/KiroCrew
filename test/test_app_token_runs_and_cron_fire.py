"""An app token is not the owner on the run routes, and an app's job never fires as a Bot.

``POST /api/spawn`` holds an app token to what the App Kit SDK holds an app's run to
(``apps.spawn_sdk``): a named agent, which the gate then proves is the app's own
because the route hands it the app; no parent session but the app's own; and a run
recorded under the app. The per-run routes (status, steer, retry, release, delete,
continue) and the live half of the listing reach only a run recorded under the
caller's app, with the 404 a caller gets for a run it may not see. The owner's own
requests, and an internal caller's session-owned reach, are as they were.

At fire time a job an app created does not collapse a Bot's name onto the Bot's
agent, workspace or crew identity, in a single run or an ``agent_sequence`` step,
and a job that captured a Bot's execution is refused. A row on the shared store
still collapses, and the owner's own job still runs as the Bot.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from handlers_messaging_coverage_helpers import _Req, _state
from member_memory_helpers import write_member_home
from test_cron_sequence_member_alias import _cron_callback

import kiro_crew.dashboard.handlers.messaging as messaging
from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, config_dir
from kiro_crew.config.sections import WorkspaceConfig
from kiro_crew.cron import CronJob
from kiro_crew.dashboard import slot_ownership
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

APP = "shop"
OTHER_APP = "other"
OWN_AGENT = "shop--helper"


def _denials(sel_calls: MagicMock) -> list[tuple[str, str]]:
    return [
        (call.kwargs["caller"], call.kwargs["operation"])
        for call in sel_calls.log_api_access.call_args_list
        if call.kwargs.get("outcome") == "denied"
    ]


@pytest.fixture
def sel_calls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import kiro_crew.sel as sel_mod

    recorder = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    # The app-isolation rows go through slot_ownership, which binds sel at import.
    monkeypatch.setattr(slot_ownership, "sel", lambda: recorder)
    monkeypatch.setattr(messaging, "_sel", lambda: recorder)
    return recorder


def _run(**fields) -> SimpleNamespace:
    """A live run record with the fields the routes read."""
    base = dict(
        id="r1",
        task="do it",
        done=False,
        error="",
        error_code="",
        result="",
        started=1_700_000_000.0,
        turns=1,
        last_tool="",
        parent_session_key="",
        agent=OWN_AGENT,
        crew="",
        app="",
        memory_store="",
        user_stopped=False,
        outcome="",
        include_memory=True,
        include_lessons=True,
        include_project=True,
    )
    base.update(fields)
    return SimpleNamespace(**base)


def _manager(*runs: SimpleNamespace) -> MagicMock:
    manager = MagicMock(max_concurrent=4, _tasks={})
    manager.all_agents = list(runs)
    manager._agents = {run.id: run for run in runs}
    manager.get.side_effect = manager._agents.get
    manager.spawn.return_value = _run(id="new")
    return manager


class _Request(_Req):
    """The request double, plus the per-request cache the scope check writes."""

    def __setitem__(self, key: str, value: object) -> None:
        self._extra[key] = value


def _request(manager, body=None, *, app: str, run_id: str = "", slots=None) -> _Request:
    state = _state(subagents=manager, _slots=slots or {})
    match = {"agent_id": run_id} if run_id else None
    return _Request(state, body, match_info=match, extra={"app": app, "internal_auth": False})


@pytest.fixture
def persistent_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parent session's memory mode, which a slot double does not carry."""
    monkeypatch.setattr(
        messaging, "_spawn_request_memory_mode", AsyncMock(return_value="persistent")
    )


def _call(handler, request: _Request) -> web.Response:
    return asyncio.run(handler(request))


# ── POST /api/spawn ──


def test_an_app_tokens_spawn_is_handed_to_the_gate_as_the_apps(sel_calls) -> None:
    manager = _manager()
    body = {"task": "summarise", "agent": OWN_AGENT, "approval_mode": "auto"}

    response = _call(messaging.api_spawn, _request(manager, body, app=APP))

    assert response.status == 200, response.text
    kwargs = manager.spawn.call_args.kwargs
    # The gate proves a named agent is the app's own only when it is told the app.
    assert (kwargs["app"], kwargs["agent"]) == (APP, OWN_AGENT)
    assert _denials(sel_calls) == []


def test_an_app_tokens_spawn_names_an_agent(sel_calls) -> None:
    manager = _manager()

    response = _call(messaging.api_spawn, _request(manager, {"task": "summarise"}, app=APP))

    assert response.status == 400
    assert json.loads(response.text)["code"] == "spawn_rejected"
    manager.spawn.assert_not_called()
    assert _denials(sel_calls) == [(APP, "spawn.create")]


@pytest.mark.parametrize(
    ("parent", "status"),
    [
        # A session the app owns, and none at all.
        ("dashboard:shop-1", 200),
        ("", 200),
        # The owner's session, another app's, and one nothing records.
        ("dashboard:mine", 409),
        ("dashboard:other-1", 409),
        ("dashboard:gone", 409),
    ],
)
def test_an_app_tokens_run_reports_only_into_a_session_of_the_apps(
    sel_calls, persistent_parent, parent, status
) -> None:
    manager = _manager()
    slots = {
        "shop-1": SimpleNamespace(key="shop-1", _app=APP, linked_session_key=""),
        "mine": SimpleNamespace(key="mine", _app="", linked_session_key=""),
        "other-1": SimpleNamespace(key="other-1", _app=OTHER_APP, linked_session_key=""),
    }
    body = {"task": "summarise", "agent": OWN_AGENT, "parent_session": parent}

    response = _call(messaging.api_spawn, _request(manager, body, app=APP, slots=slots))

    assert response.status == status, response.text
    if status == 200:
        assert manager.spawn.call_args.kwargs["parent_session_key"] == parent
    else:
        assert json.loads(response.text)["code"] == "member_identity_unavailable"
        manager.spawn.assert_not_called()
        assert _denials(sel_calls) == [(APP, "spawn.create")]


@pytest.mark.parametrize("parent", ["cron:deadbeef", "cron:deadbeef:run1", "subagent:deadbeef"])
@pytest.mark.parametrize("record_app", [None, "", OTHER_APP, APP])
@pytest.mark.parametrize("linked", [False, True])
def test_parent_namespace_uses_its_registry_not_a_colliding_slot(
    sel_calls, persistent_parent, parent, record_app, linked
) -> None:
    runs = [] if record_app is None else [_run(id="deadbeef", app=record_app)]
    manager = _manager(*runs)
    jobs = (
        []
        if record_app is None
        else [SimpleNamespace(id="deadbeef", created_by=f"app:{record_app}" if record_app else "")]
    )
    slot_key = "alias" if linked else parent.split(":", 1)[1]
    slots = {
        slot_key: SimpleNamespace(
            key=slot_key, _app=APP, linked_session_key=parent if linked else ""
        )
    }
    body = {"task": "summarise", "agent": OWN_AGENT, "parent_session": parent}
    request = _request(manager, body, app=APP, slots=slots)
    request.app["state"].crons = SimpleNamespace(_jobs=jobs)

    response = _call(messaging.api_spawn, request)

    if record_app == APP:
        assert response.status == 200, response.text
        assert manager.spawn.call_args.kwargs["parent_session_key"] == parent
        assert _denials(sel_calls) == []
    else:
        assert response.status == 409, response.text
        assert json.loads(response.text) == {
            "error": "The execution identity is unavailable; Global memory was not used.",
            "code": "member_identity_unavailable",
        }
        manager.spawn.assert_not_called()
        assert _denials(sel_calls) == [(APP, "spawn.create")]


@pytest.mark.parametrize(
    ("parent", "slot_app", "linked_key", "status"),
    [
        ("dashboard:shop-1", APP, "", 200),
        ("dashboard:shop-1", APP, "slack:channel:thread", 409),
        ("dashboard:missing", APP, "dashboard:missing", 409),
        ("slack:channel:thread", APP, "slack:channel:thread", 200),
        ("slack:channel:thread", OTHER_APP, "slack:channel:thread", 409),
        ("slack:channel:thread", APP, "slack:channel:other", 409),
        ("slack:shop-1", APP, "", 409),
    ],
)
def test_parent_slot_must_bind_the_exact_session_key(
    sel_calls, persistent_parent, parent, slot_app, linked_key, status
) -> None:
    manager = _manager()
    slots = {"shop-1": SimpleNamespace(key="shop-1", _app=slot_app, linked_session_key=linked_key)}
    body = {"task": "summarise", "agent": OWN_AGENT, "parent_session": parent}

    response = _call(messaging.api_spawn, _request(manager, body, app=APP, slots=slots))

    assert response.status == status, response.text
    if status == 200:
        assert manager.spawn.call_args.kwargs["parent_session_key"] == parent
        assert _denials(sel_calls) == []
    else:
        assert json.loads(response.text)["code"] == "member_identity_unavailable"
        manager.spawn.assert_not_called()
        assert _denials(sel_calls) == [(APP, "spawn.create")]


@pytest.mark.parametrize("route", ["continue", "lost", "mark_collected"])
def test_run_controls_refuse_a_parent_with_only_a_colliding_app_slot(sel_calls, route) -> None:
    manager = _manager(_run(id="r1", app=APP, done=True))
    slots = {"deadbeef": SimpleNamespace(key="deadbeef", _app=APP, linked_session_key="")}
    body = {
        "task": "again",
        "batch_id": "b1",
        "ids": ["r1"],
        "written": True,
        "parent_session": "cron:deadbeef",
    }
    request = _request(manager, body, app=APP, run_id="r1", slots=slots)
    request.app["state"].crons = SimpleNamespace(_jobs=[])

    response = _call(getattr(messaging, f"api_spawn_{route}"), request)

    assert response.status == 409, response.text
    assert json.loads(response.text)["code"] == "member_identity_unavailable"
    manager.continue_conversation.assert_not_called()
    manager.record_lost_submission.assert_not_called()
    manager.mark_collected.assert_not_called()
    operation = "spawn.continue" if route == "continue" else "spawn.batch"
    assert _denials(sel_calls) == [(APP, operation)]


def test_the_owners_spawn_is_not_an_apps(sel_calls) -> None:
    """The owner names no agent and is handed to the gate with no app."""
    manager = _manager()

    response = _call(messaging.api_spawn, _request(manager, {"task": "summarise"}, app=""))

    assert response.status == 200, response.text
    assert "app" not in manager.spawn.call_args.kwargs
    assert _denials(sel_calls) == []


# ── the per-run routes ──


@pytest.mark.parametrize("route", ["status", "steer", "retry", "release", "delete", "continue"])
@pytest.mark.parametrize("run_app", ["", OTHER_APP])
def test_an_app_token_reaches_no_run_it_did_not_start(sel_calls, route, run_app) -> None:
    manager = _manager(_run(id="r1", app=run_app, done=route in ("retry", "continue")))
    handler = getattr(messaging, f"api_spawn_{route}")
    body = {"task": "again", "message": "stop"}

    response = _call(handler, _request(manager, body, app=APP, run_id="r1"))

    assert response.status == 404
    assert json.loads(response.text) == {"error": "not found", "code": "task_scope_denied"}
    assert _denials(sel_calls) == [(APP, "spawn.access")]
    manager.spawn.assert_not_called()
    manager.steer.assert_not_called()
    manager.cancel.assert_not_called()


@pytest.mark.parametrize(
    ("caller_app", "run_app"),
    [
        # An app's own run, and the owner on anyone's run.
        (APP, APP),
        ("", ""),
        ("", OTHER_APP),
    ],
)
def test_the_scope_check_admits_the_runs_app_and_the_owner(sel_calls, caller_app, run_app) -> None:
    manager = _manager(_run(id="r1", app=run_app))
    request = _request(manager, None, app=caller_app, run_id="r1")

    assert asyncio.run(messaging._spawn_scope_refusal(request)) is None
    assert _denials(sel_calls) == []


@pytest.mark.parametrize(("record_app", "admitted"), [(APP, True), (OTHER_APP, False), ("", False)])
def test_a_finished_run_is_the_apps_by_its_record(
    monkeypatch, sel_calls, record_app, admitted
) -> None:
    """A run this process does not hold: its own record names the app."""
    manager = _manager()
    manager._admission.queued_run = None
    monkeypatch.setattr(messaging, "_queued_run", AsyncMock(return_value=None))
    record = {"parent_session": "", "execution_context": {"app": record_app}}
    monkeypatch.setattr(messaging, "read_state", lambda run_id: record)
    request = _request(manager, None, app=APP, run_id="old")

    refusal = asyncio.run(messaging._spawn_scope_refusal(request))

    assert (refusal is None) is admitted
    if not admitted:
        assert refusal.status == 404


def test_an_app_token_lists_only_the_live_runs_of_its_app(monkeypatch, sel_calls) -> None:
    manager = _manager(
        _run(id="mine", app=APP), _run(id="owners", app=""), _run(id="theirs", app=OTHER_APP)
    )
    monkeypatch.setattr(
        messaging, "read_panel_records", lambda **kw: messaging.PanelRecords([], 0, False)
    )

    listed = {
        caller: [
            row["id"]
            for row in json.loads(
                _call(messaging.api_spawn_list, _request(manager, None, app=caller)).text
            )["agents"]
        ]
        for caller in (APP, "")
    }

    assert listed == {APP: ["mine"], "": ["mine", "owners", "theirs"]}


@pytest.mark.parametrize("route", ["lost", "mark_collected"])
def test_an_app_token_reconciles_no_batch_of_another_session(sel_calls, route) -> None:
    manager = _manager()
    slots = {"mine": SimpleNamespace(_app="", linked_session_key="")}
    body = {"batch_id": "b1", "ids": ["r1"], "written": True, "parent_session": "dashboard:mine"}
    handler = getattr(messaging, f"api_spawn_{route}")

    response = _call(handler, _request(manager, body, app=APP, slots=slots))

    assert response.status == 409
    assert json.loads(response.text)["code"] == "member_identity_unavailable"
    manager.record_lost_submission.assert_not_called()
    assert _denials(sel_calls) == [(APP, "spawn.batch")]


# ── cron fire ──


async def _fire(monkeypatch, tmp_path, *, created_by: str, **job_fields):
    """Fire one job against a config with a Bot, ``beta``, and a shared-store row."""
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    beta_tree = tmp_path / "beta-tree"
    beta_tree.mkdir()
    cfg.workspaces["beta-ws"] = WorkspaceConfig(dir=str(beta_tree))
    cfg.agents["beta"].kiro_agent = "beta-mode"
    cfg.agents["beta"].workspace = "beta-ws"
    cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="plain-mode")
    gw, callback = await _cron_callback(monkeypatch, cfg)
    gw._alert_cron_failure = AsyncMock()
    # A job stores the agent it names as its captured template; the fire
    # dispatches that name, not ``agent_id``.
    named = job_fields.get("agent_id") or "kirocrew"
    template = ExecutionContext(None, MemoryStoreRef("default"), "template", named)
    job = CronJob(
        id="job1",
        name="job",
        message="task",
        created_by=created_by,
        **{"execution_context": template.to_record(), **job_fields},
    )
    try:
        await callback(job)
    except RuntimeError as exc:
        # The harness stops every dispatch at the provider.
        assert "stop before the provider" in str(exc)
    return gw, job, str(beta_tree)


def _dispatches(gw) -> list[tuple[str | None, str | None, str | None]]:
    return [
        (call.kwargs.get("agent"), call.kwargs.get("crew_agent"), call.kwargs.get("cwd"))
        for call in gw.sessions.get_or_create.call_args_list
    ]


@pytest.mark.asyncio
async def test_an_apps_sequence_step_naming_a_bot_runs_as_a_template(monkeypatch, tmp_path):
    gw, _job, beta_tree = await _fire(
        monkeypatch, tmp_path, created_by=f"app:{APP}", agent_sequence=["beta", "plain"]
    )

    agent, crew, cwd = _dispatches(gw)[0]
    # The name as a template, no crew identity, and not the Bot's workspace.
    assert (agent, crew) == ("beta", "")
    assert cwd != beta_tree


@pytest.mark.asyncio
async def test_the_owners_sequence_step_still_runs_as_the_bot(monkeypatch, tmp_path):
    gw, _job, beta_tree = await _fire(
        monkeypatch, tmp_path, created_by="", agent_sequence=["beta", "plain"]
    )

    assert _dispatches(gw)[0] == ("beta-mode", "beta", beta_tree)


@pytest.mark.asyncio
async def test_an_apps_sequence_step_on_a_shared_store_row_still_collapses(monkeypatch, tmp_path):
    gw, _job, _tree = await _fire(
        monkeypatch, tmp_path, created_by=f"app:{APP}", agent_sequence=["plain", "beta"]
    )

    agent, crew, _cwd = _dispatches(gw)[0]
    assert (agent, crew) == ("plain-mode", "plain")


@pytest.mark.asyncio
async def test_an_apps_single_job_naming_a_bot_takes_nothing_of_the_bots(monkeypatch, tmp_path):
    gw, _job, beta_tree = await _fire(
        monkeypatch, tmp_path, created_by=f"app:{APP}", agent_id="beta"
    )

    agent, crew, cwd = _dispatches(gw)[0]
    # The name as a template, no crew identity, and not the Bot's workspace.
    assert (agent, crew) == ("beta", "")
    assert cwd != beta_tree


@pytest.mark.asyncio
async def test_the_owners_single_job_still_runs_as_the_bot(monkeypatch, tmp_path):
    gw, _job, beta_tree = await _fire(monkeypatch, tmp_path, created_by="", agent_id="beta")

    assert _dispatches(gw)[0] == ("beta-mode", "beta", beta_tree)


@pytest.mark.asyncio
@pytest.mark.parametrize(("created_by", "refused"), [(f"app:{APP}", True), ("", False)])
async def test_an_apps_job_that_captured_a_bots_execution_is_refused(
    monkeypatch, tmp_path, created_by, refused
):
    captured = ExecutionContext(
        "beta", MemoryStoreRef("member-beta", "beta"), "member", "beta-mode", selection_name="beta"
    )

    gw, job, _tree = await _fire(
        monkeypatch,
        tmp_path,
        created_by=created_by,
        member_id="beta",
        execution_context=captured.to_record(),
    )

    from kiro_crew.slack import gateway

    if refused:
        gw.sessions.get_or_create.assert_not_awaited()
        assert (job.last_status, job.last_error) == ("error", gateway.APP_JOB_BOT_REFUSAL)
        assert job.fire_time_denied is True
        gw._alert_cron_failure.assert_awaited_once()
    else:
        assert _dispatches(gw)[0][1] == "beta"
        assert job.fire_time_denied is False
