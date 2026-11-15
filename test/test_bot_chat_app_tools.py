"""App tools in a Bot's own chat (``kiro_crew.bot_chat_app_tools``).

Each guarantee below has one test, and the one-line change that reddens it:

* grant mounts: an app holding the ``tools`` scope mounts its offer on a ticked Bot's
  chat spec with the label ``dashboard:<slot>`` -- drop ``mounted[alias] = entry``;
* preview gate: preview off mounts nothing -- make ``preview_on`` return True;
* scope gate: no approved ``tools`` scope mounts nothing -- drop the
  ``BOT_SCOPE_TOOLS in app_bot_scopes(app)`` half of ``_app_reaches``;
* per Bot: a Bot the owner did not tick mounts nothing -- drop the
  ``slug in app_granted_bots(app)`` half of ``_app_reaches``;
* offer gate: a server not offered by the app's own agent mounts nothing -- drop
  ``if item["agent"] in own`` in ``_offers``;
* platform wins: a same-named platform server is not replaced -- drop ``if alias in mounted``;
* array hosts: ``session_mcp_projection`` carries it for the conductor and only for
  it -- drop its ``_crewmate_conductor_spec`` call / the agent check there;
* the manifest scope parses -- drop ``"tools"`` from ``BOT_SCOPE_NAMES``;
* a change: Bots that lost the app start a fresh conversation, Bots that gained it
  reload -- swap the two branches of ``after_app_change``;
* unticking a Bot on the owner route starts its chat afresh -- drop the route's
  ``after_app_change`` call;
* a reload skips a chat whose fresh start is queued (disable then enable), so one chat
  never spawns two agent processes at once -- drop the ``_pending_discard...`` guard;
* the fresh start is the queued no-replay discard -- drop the
  ``slot._pending_discard_conversation_key = ...`` line.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew.apps import bot_grants

APP = "team-app"
SLOT = "member-atlas.memory-member-atlas-1"
KEY = f"dashboard:{SLOT}"
ENTRY = {"command": "python3", "args": ["/a/server.py"], "x-kirocrew-session-label": True}
BASE = {"tools": ["fs_read", "@kirocrew-core"], "mcpServers": {"kirocrew-core": {"command": "k"}}}


@pytest.fixture
def world(monkeypatch):
    """One enabled app offering ``team`` from its agent ``lead``, the Bot ``atlas`` ticked."""
    state = {
        "preview": True,
        "scopes": {"tools"},
        "ticked": ("atlas",),
        "offers": [("lead", "team")],
    }
    monkeypatch.setattr(tools, "preview_on", lambda cfg=None: state["preview"])
    monkeypatch.setattr(tools, "_app_names", lambda: [APP])
    monkeypatch.setattr(tools, "_offers", lambda app: list(state["offers"]))
    monkeypatch.setattr(tools, "_rendered_entry", lambda *a: dict(ENTRY))
    monkeypatch.setattr(bot_grants, "app_bot_scopes", lambda app: frozenset(state["scopes"]))
    monkeypatch.setattr(bot_grants, "app_granted_bots", lambda app: tuple(state["ticked"]))
    import kiro_crew.members as members

    monkeypatch.setattr(
        members,
        "member_identity_for_session",
        lambda key: {"slug": key.split("member-")[1].split(".")[0], "name": "Atlas"},
    )
    return state


def test_a_ticked_bot_chat_mounts_the_offer_with_its_label(world):
    out = tools.conductor_session_spec(dict(BASE), KEY)
    entry = out["mcpServers"][f"{APP}:team"]
    assert f"@{APP}:team" in out["tools"]
    assert entry["env"]["KIROCREW_SESSION_LABEL"] == KEY
    assert "x-kirocrew-session-label" not in entry
    assert BASE["mcpServers"].keys() == {"kirocrew-core"}  # the shared spec is not written


def test_preview_off_mounts_nothing(world):
    world["preview"] = False
    assert tools.conductor_session_spec(dict(BASE), KEY) == BASE


def test_without_the_approved_tools_scope_nothing_mounts(world):
    world["scopes"] = {"read", "wake"}
    assert tools.conductor_session_spec(dict(BASE), KEY) == BASE


def test_a_bot_the_owner_did_not_tick_mounts_nothing(world):
    world["ticked"] = ("nova",)
    assert tools.conductor_session_spec(dict(BASE), KEY) == BASE


def test_only_a_server_the_apps_own_agent_declares_is_offered(monkeypatch):
    from kiro_crew.apps import manager
    from kiro_crew.apps.manifest import AppManifest

    manifest = AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "agents": ["agents/lead.json"],
            "contributes": {
                "botChatServers": [
                    {"agent": "lead", "server": "team"},
                    {"agent": "someone-else", "server": "x"},
                ]
            },
        }
    )
    monkeypatch.setattr(manager, "get_app_manifest", lambda name: manifest)
    assert tools._offers(APP) == [("lead", "team")]


def test_a_platform_server_of_the_same_name_wins(world, monkeypatch):
    monkeypatch.setattr(tools, "bot_servers", lambda slug: {"kirocrew-core": dict(ENTRY)})
    out = tools.conductor_session_spec(dict(BASE), KEY)
    assert out["mcpServers"]["kirocrew-core"] == {"command": "k"}


def test_array_host_projection_carries_it_for_the_conductor_only(world, monkeypatch):
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(
        session_mcp, "_session_spec_and_snapshot_for", lambda agent, wd: (dict(BASE), None)
    )
    projection = session_mcp.session_mcp_projection("kirocrew-mate-conductor", session_key=KEY)
    assert f"{APP}:team" in projection.agent_spec["mcpServers"]
    other = session_mcp.session_mcp_projection("kiro_default", session_key=KEY)
    assert f"{APP}:team" not in (other.agent_spec or {}).get("mcpServers", {})


def test_the_manifest_declares_the_tools_scope():
    from kiro_crew.apps.manifest_sections import Permissions

    assert Permissions.from_dict({"bots": ["tools", "nope"]}).bots == ["tools"]


def test_manifest_offer_round_trips_and_a_bad_shape_is_an_error():
    from kiro_crew.apps.manifest_contributes import Contributes

    good = Contributes.from_dict({"botChatServers": [{"agent": "lead", "server": "team"}]})
    assert good.to_dict()["botChatServers"] == [{"agent": "lead", "server": "team"}]
    assert good.validate() == []
    bad = Contributes.from_dict({"botChatServers": [{"agent": "lead"}]})
    assert any("botChatServers" in e for e in bad.validate())


def _watch(monkeypatch):
    seen: dict[str, list[list[str]]] = {"reload": [], "fresh": []}
    monkeypatch.setattr(tools, "schedule_reload", lambda state, slugs: seen["reload"].append(slugs))

    async def _fresh(state, slugs):
        seen["fresh"].append(list(slugs))
        return list(slugs)

    monkeypatch.setattr(tools, "fresh_bot_chats", _fresh)
    return seen


@pytest.mark.asyncio
async def test_a_change_starts_lost_bots_afresh_and_reloads_gained_ones(world, monkeypatch):
    seen = _watch(monkeypatch)
    world["ticked"] = ("nova",)
    await tools.after_app_change(object(), APP, frozenset({"atlas"}))
    assert seen == {"reload": [["nova"]], "fresh": [["atlas"]]}


@pytest.mark.asyncio
async def test_a_disabled_app_starts_its_bots_afresh(world, monkeypatch):
    seen = _watch(monkeypatch)
    world["scopes"] = set()  # ``app_bot_scopes`` is empty for a disabled app
    await tools.after_app_change(object(), APP, frozenset({"atlas"}))
    assert seen == {"reload": [], "fresh": [["atlas"]]}


@pytest.mark.asyncio
async def test_unticking_a_bot_on_the_owner_route_starts_its_chat_afresh(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from chat_test_helpers import _make_state

    from kiro_crew.apps import manager_records
    from kiro_crew.apps.manager_records import InstalledApp
    from kiro_crew.apps.routes_bot_grants import handle_bot_grants

    # The real ticked-list store, so the route's before and after reads differ.
    monkeypatch.setattr(tools, "preview_on", lambda cfg=None: True)
    monkeypatch.setattr(tools, "_offers", lambda app: [("lead", "team")])
    monkeypatch.setattr(bot_grants, "app_bot_scopes", lambda app: frozenset({"tools"}))
    seen = _watch(monkeypatch)
    manager_records.app_dir(APP).mkdir(parents=True, exist_ok=True)
    manager_records._write_installed(
        APP, InstalledApp(name=APP, version="1.0.0", enabled=True, installedAt="2026-11-15T00:00Z")
    )
    bot_grants.set_app_granted_bots(APP, ["atlas"])
    state = _make_state(tmp_path)
    state.owner_id = "U_OWNER"
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(
        "PUT", f"/api/apps/{APP}/bot-grants", app=app, match_info={"name": APP}
    )
    req["app"] = ""
    req["user"] = "U_OWNER"

    async def _json():
        return {"bots": []}

    req.json = _json  # type: ignore[method-assign]
    resp = await handle_bot_grants(req)
    assert resp.status == 200 and json.loads(resp.body)["bots"] == []
    assert seen == {"reload": [], "fresh": [["atlas"]]}


@pytest.mark.asyncio
async def test_fresh_bot_chat_queues_a_no_replay_discard_on_its_slot(monkeypatch):
    import kiro_crew.dashboard.chat_turn.session_reset as reset

    armed: list[object] = []
    monkeypatch.setattr(reset, "_arm_pending_reset_retry", lambda state, slot: armed.append(slot))
    rows: list[tuple] = []
    slot = SimpleNamespace(
        key=SLOT,
        _pending_discard_conversation_key=None,
        _pending_discard_from_wake=True,
        append=lambda *row: rows.append(row),
    )
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slot if slug == "atlas" else None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )
    assert await tools.fresh_bot_chats(object(), ["atlas", "gone"]) == ["atlas"]
    assert slot._pending_discard_conversation_key == f"dashboard_{SLOT}"
    assert slot._pending_discard_from_wake is False
    assert armed and armed[0] is slot
    assert rows and rows[0][0] == "system"


@pytest.mark.asyncio
async def test_reload_reconnects_an_idle_chat_and_leaves_a_queued_fresh_start_alone(monkeypatch):
    import kiro_crew.dashboard.crewmate_worker_reload as reload_mod

    calls: list[tuple[str, str]] = []

    async def _reload(state, slot, *, notice):
        calls.append((slot.key, notice))
        return reload_mod.RELOADED

    monkeypatch.setattr(reload_mod, "reload_if_idle", _reload)
    slots = {
        "ada": SimpleNamespace(key="s-ada", _pending_discard_conversation_key=None),
        "uma": SimpleNamespace(key="s-uma", _pending_discard_conversation_key="dashboard_s-uma"),
    }
    monkeypatch.setattr(tools, "_live_slot", lambda state, slug: slots.get(slug))
    out = await tools.reload_bot_chats(object(), ["ada", "uma"])
    assert out == {"ada": reload_mod.RELOADED, "uma": tools.PENDING_FRESH}
    assert calls == [("s-ada", tools._RELOAD_NOTICE)]
