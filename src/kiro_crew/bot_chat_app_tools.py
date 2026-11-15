"""App tools in a Bot's own chat: the servers an app offers, on the Bots its owner ticked.

A Bot's chat runs on the shared ``kirocrew-mate-conductor``, which mounts only the
platform servers (``mate_worker.conductor_mcp_servers``). This module is the one
exception (recorded in ``docs/decisions``), and it adds nothing unless ALL of these hold:

* the feature preview ``bot_chat_app_tools`` is on;
* the app is installed and enabled;
* the app OFFERS the server in ``contributes.botChatServers`` (each ``{agent,
  server}`` names a server one of the app's own agents declares);
* the owner approved the app's ``tools`` Bot scope (``permissions.bots``, a staged
  grant: ``apps.bot_grants.app_bot_scopes``);
* the owner ticked THIS Bot for the app (``apps.bot_grants.app_granted_bots``).

The offered server joins this Bot's chat spec per session
(:func:`conductor_session_spec`), the way a Bot's worker gets its own spec per
session (``mate_worker.worker_session_spec``); the shared conductor file is never
written. Its entry carries ``KIROCREW_SESSION_LABEL = dashboard:<the chat's slot>``,
so the app's server knows which Bot is calling. Approvals stay the Bot's own:
nothing here grants a tool call.

A chat mounts its servers when its agent process starts, so a change reaches a live
chat through :func:`after_app_change`: a Bot that gains a server reloads (same
conversation), one that loses it starts a fresh conversation, so the old tool names
leave its context too.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

#: ``dashboard.feature_previews.<this>``.
PREVIEW = "bot_chat_app_tools"


def preview_on(cfg: Any = None) -> bool:
    """Whether the preview is on. Off on any read failure."""
    try:
        if cfg is None:
            from kiro_crew.config.loader import KiroCrewConfig

            cfg = KiroCrewConfig.load()
        previews = getattr(getattr(cfg, "dashboard", None), "feature_previews", None)
    except Exception:  # noqa: BLE001 - an unread config is the preview off
        return False
    return isinstance(previews, dict) and previews.get(PREVIEW) is True


def _agent_stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".json")


def _offers(app: str) -> list[tuple[str, str]]:
    """``[(agent, server)]`` *app* offers to Bot chats, its own agents only. Blocking."""
    from kiro_crew.apps.manager import get_app_manifest

    manifest = get_app_manifest(app)
    if manifest is None:
        return []
    own = {_agent_stem(p) for p in manifest.agents}
    return [
        (item["agent"], item["server"])
        for item in manifest.contributes.botChatServers
        if item["agent"] in own
    ]


def _app_reaches(app: str, slug: str) -> bool:
    """Whether *app* holds the approved ``tools`` scope AND the owner ticked Bot *slug*."""
    from kiro_crew.apps.bot_grants import BOT_SCOPE_TOOLS, app_bot_scopes, app_granted_bots

    # ``app_bot_scopes`` is empty for an app that is not installed and enabled.
    return BOT_SCOPE_TOOLS in app_bot_scopes(app) and slug in app_granted_bots(app)


def _app_names() -> list[str]:
    from kiro_crew.apps.manager import list_apps

    return [str(a.get("name") or "") for a in list_apps() if isinstance(a, dict)]


def _rendered_entry(app: str, agent: str, server: str) -> dict[str, Any] | None:
    """The server's entry in the app agent's RENDERED spec (paths resolved, governed)."""
    from kiro_crew.apps.bridges_sources import _kiro_agents_dir, _namespace, _safe_link_name

    path = _kiro_agents_dir() / f"{_safe_link_name(_namespace(app, agent))}.json"
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    entry = servers.get(server) if isinstance(servers, dict) else None
    return dict(entry) if isinstance(entry, dict) else None


def bot_servers(slug: str, cfg: Any = None) -> dict[str, dict[str, Any]]:
    """``{alias: entry}`` Bot *slug*'s chat mounts now. Empty with the preview off. Blocking."""
    from kiro_crew.mcp_utils import mcp_server_alias

    if not slug or not preview_on(cfg):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for app in _app_names():
        try:
            if not app or not _app_reaches(app, slug):
                continue
            for agent, server in _offers(app):
                entry = _rendered_entry(app, agent, server)
                if entry is not None:
                    out[mcp_server_alias(f"{app}:{server}")] = entry
        except Exception:  # noqa: BLE001 - one bad app must not hide the rest
            logger.warning("bot chat tools: app %s unreadable", app, exc_info=True)
    return out


def mounting_bots(app: str, cfg: Any = None) -> frozenset[str]:
    """The Bot slugs whose chat mounts a server of *app* now. Empty on any failure. Blocking."""
    try:
        if not preview_on(cfg) or not _offers(app):
            return frozenset()
        from kiro_crew.apps.bot_grants import BOT_SCOPE_TOOLS, app_bot_scopes, app_granted_bots

        if BOT_SCOPE_TOOLS not in app_bot_scopes(app):
            return frozenset()
        return frozenset(app_granted_bots(app))
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("bot chats using %s unreadable", app, exc_info=True)
        return frozenset()


def _slot_of(session_key: str) -> str:
    for prefix in ("dashboard_", "dashboard:"):
        if session_key.startswith(prefix):
            return session_key[len(prefix) :]
    return session_key


def conductor_session_spec(spec: dict[str, Any], session_key: str) -> dict[str, Any]:
    """*spec* (the shared conductor's) plus the app servers this Bot's chat mounts.

    Never raises and never narrows: anything unreadable answers *spec* unchanged. A
    platform server of the same name wins. The session-label marker is replaced by
    the label itself, so every harness hands the server the same caller. Blocking.
    """
    from kiro_crew.agent_sdk.agent_specs import stamp_session_label
    from kiro_crew.members import member_identity_for_session

    try:
        ident = member_identity_for_session(session_key)
        servers = bot_servers(ident["slug"]) if ident else {}
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("bot chat tools for %s unreadable", session_key, exc_info=True)
        return spec
    if not servers:
        return spec
    out = dict(spec)
    mounted = dict(out.get("mcpServers") or {})
    tools = list(out.get("tools") or [])
    label = f"dashboard:{_slot_of(session_key)}"
    for alias, entry in servers.items():
        if alias in mounted:
            continue
        mounted[alias] = stamp_session_label(entry, label)
        if "*" not in tools and f"@{alias}" not in tools:
            tools.append(f"@{alias}")
    out["mcpServers"] = mounted
    out["tools"] = tools
    return out


def _live_slot(state: Any, slug: str) -> Any:
    from kiro_crew.members import read_dm_binding

    binding = read_dm_binding(slug)
    return state._slots.get(binding["slot_key"]) if binding else None


#: :func:`reload_bot_chats` outcome for a chat whose queued fresh start covers it.
PENDING_FRESH = "pending_fresh"

_RELOAD_NOTICE = (
    "Session reloaded so this Bot's chat picks up the app tools its owner turned on. "
    "The conversation is kept."
)

_FRESH_NOTICE = (
    "App tools were taken off this Bot's chat, so its next turn starts a fresh "
    "conversation without them. The messages above stay here."
)


async def reload_bot_chats(state: Any, slugs: Iterable[str]) -> dict[str, str]:
    """Reload each Bot's live chat onto its spec now (idle) or at its turn end (busy).

    So a chat picks up an app server on its next turn. Never raises.
    """
    from kiro_crew.dashboard.crewmate_worker_reload import reload_if_idle

    out: dict[str, str] = {}
    for slug in slugs:
        try:
            slot = await asyncio.to_thread(_live_slot, state, slug)
            if slot is None:
                continue
            if getattr(slot, "_pending_discard_conversation_key", None) is not None:
                # A fresh start is already queued (an app disabled then enabled): its
                # spawn reads the new spec, and a reload beside it would start a second
                # agent process on the same chat at once.
                out[slug] = PENDING_FRESH
                continue
            out[slug] = await reload_if_idle(state, slot, notice=_RELOAD_NOTICE)
        except Exception:  # noqa: BLE001 - a missed reload leaves the next spawn to read it
            logger.warning("bot chat reload for %s failed", slug, exc_info=True)
    return out


async def fresh_bot_chats(state: Any, slugs: Iterable[str]) -> list[str]:
    """Queue a fresh conversation on each Bot's live chat; returns the Bots queued.

    For a server taken AWAY. A reload resumes the same conversation (``session/load``),
    and that history still names the removed tools, so the model keeps offering them.
    This queues the discard the ``reset_conversation`` directive queues
    (``slot._pending_discard_conversation_key``): it lands at the next idle turn
    boundary with ``replay=False``, and the next turn spawns on the narrowed spec with
    no memory of the old tools. The transcript stays in the tab. Never raises.
    """
    from kiro_crew.dashboard.chat_turn.session_reset import _arm_pending_reset_retry
    from kiro_crew.dashboard.chat_utils import effective_session_key

    queued: list[str] = []
    for slug in slugs:
        try:
            slot = await asyncio.to_thread(_live_slot, state, slug)
            # shortcut: a chat with no live slot keeps its resume pointer; every opened
            # Bot chat has a slot while the gateway runs, so only a restart lands here.
            if slot is None:
                continue
            slot._pending_discard_conversation_key = effective_session_key(slot)
            slot._pending_discard_from_wake = False
            slot.append("system", _FRESH_NOTICE, "msg msg-info")
            _arm_pending_reset_retry(state, slot)
            queued.append(slug)
        except Exception:  # noqa: BLE001 - a missed discard leaves the tools uncallable
            logger.warning("bot chat fresh start for %s failed", slug, exc_info=True)
    return queued


async def snapshot(app: str) -> frozenset[str]:
    """:func:`mounting_bots` off the loop: call it BEFORE a change to *app*."""
    return await asyncio.to_thread(mounting_bots, app)


async def after_app_change(state: Any, app: str, before: frozenset[str]) -> None:
    """After a change to *app*: the Bots that lost its servers start fresh, the rest reload.

    *before* is :func:`snapshot` taken before the change (enable, disable, uninstall,
    a new ticked list). Never raises.
    """
    if state is None:
        return
    try:
        after = await asyncio.to_thread(mounting_bots, app)
    except Exception:  # noqa: BLE001 - the app change has committed
        logger.warning("bot chats using %s unreadable", app, exc_info=True)
        return
    lost = sorted(before - after)
    gained = sorted(after - before)
    if lost:
        await fresh_bot_chats(state, lost)
    if gained:
        schedule_reload(state, gained)


def schedule_reload(state: Any, slugs: list[str]) -> "asyncio.Task | None":
    """:func:`reload_bot_chats` as a task, kept alive on *state*."""
    if state is None or not slugs:
        return None
    try:
        task = asyncio.get_running_loop().create_task(reload_bot_chats(state, slugs))
    except RuntimeError:
        return None
    tasks = getattr(state, "_crewmate_reload_tasks", None)
    if not isinstance(tasks, set):
        tasks = set()
        state._crewmate_reload_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task
