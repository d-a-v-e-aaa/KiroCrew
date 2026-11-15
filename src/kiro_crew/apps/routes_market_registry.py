"""Kiro Space stores as App Store sources: add, sign in, list, open one listing.

Five routes, all owner-only through the same gate as ``POST /api/apps/install``
(:func:`require_owner_dashboard_request`, run before the body is read):

* ``GET  /api/apps/market/registries`` -- the offered stores, the added ids and
  each added store's sign-in status;
* ``PUT  /api/apps/market/registries`` -- ``{"added": [<id>, ...]}``, ids the
  provider offers only (``400 market_registry_unknown`` otherwise);
* ``POST /api/apps/market/registries/{id}/signin`` and ``.../signout`` -- the
  provider's own sign-in; the body must be ``{}``;
* ``GET  /api/apps/market/listings?registry=<id>`` -- the store's apps as card
  rows (``refresh=1`` drops this store's cached copy first);
* ``GET  /api/apps/market/listing?registry=<id>&ref=<ns>/<name>`` -- one listing.

With no provider (``app["market_registries"]`` unset, as in the open-source
build) the read answers ``offers: []`` and every other route answers
``503 market_registry_unavailable``. Every route that names a store first runs
:func:`~kiro_crew.apps.market_registries.require_usable_registry`.

Store calls run in the executor: the client is synchronous. Nothing a route
logs or answers carries a token, an ``Authorization`` header or the store CLI's
output; the audit records the operation, the store id, a ref and a result code.

Part of the ``kiro_crew.apps.routes`` family; this module never imports the
facade. Registered before the ``/api/apps/{name}`` catch-all.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.market_registries import (
    MARKET_LISTING_CACHE_KEY,
    MAX_ADDED,
    NEEDS_SIGN_IN,
    NO_ACCESS,
    SIGNED_IN,
    ListingCache,
    added_ids_from,
    fetch_listings,
    installed_by_name,
    listing_cache,
    listing_row,
    make_market_client_factory,
    normalize_status,
    offered,
    provider_of,
    require_usable_registry,
    sign_in_view,
    write_added_ids,
)
from kiro_crew.apps.market_source import LISTING_REF_RE
from kiro_crew.apps.marketplace_install import MarketInstallError
from kiro_crew.apps.routes_market_install import MARKET_CLIENT_FACTORY_KEY
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")


def _store_error(status: int, code: str, message: str, **extra: Any) -> web.Response:
    return web.json_response({"ok": False, "error": message, "code": code, **extra}, status=status)


def _store_unavailable(exc: MarketInstallError | None = None) -> web.Response:
    return _store_error(
        503,
        exc.code if exc is not None else "market_registry_unavailable",
        exc.message if exc is not None else "no Kiro Space store is available",
    )


def _store_audit(operation: str, outcome: str, resources: str) -> None:
    sel().log_api_access(
        caller="dashboard", operation=operation, outcome=outcome, resources=resources
    )


async def _store_gate(request: web.Request, operation: str) -> web.Response | None:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


async def _store_added_ids() -> list[str]:
    from kiro_crew.config.loader import KiroCrewConfig

    return added_ids_from(await asyncio.to_thread(KiroCrewConfig.load))


async def _store_off_loop(fn: Any, *args: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), partial(fn, *args)
    )


async def _store_status(provider: Any, registry_id: str, cache: ListingCache) -> dict[str, Any]:
    """The store's status; *cache* drops its rows when the status calls for it."""
    try:
        status = normalize_status(await _store_off_loop(provider.status, registry_id))
    except Exception as exc:  # noqa: BLE001 - a failed probe reads as unknown
        logger.warning("Kiro Space store status failed: %s", type(exc).__name__)
        status = normalize_status(None)
    cache.observe(registry_id, status)
    return status


async def _store_usable(
    request: web.Request, registry_id: object
) -> tuple[Any, str] | web.Response:
    provider = provider_of(request.app)
    try:
        rid = require_usable_registry(provider, registry_id, await _store_added_ids())
    except MarketInstallError as exc:
        return _store_unavailable(exc)
    return provider, rid


async def handle_market_registries_get(request: web.Request) -> web.Response:
    """GET /api/apps/market/registries -- offers, added ids, each added store's status."""
    denied = await _store_gate(request, "market_registries.read")
    if denied is not None:
        return denied
    provider = provider_of(request.app)
    offers = offered(provider)
    offered_ids = {o["id"] for o in offers}
    added = [i for i in await _store_added_ids() if i in offered_ids]
    statuses = {}
    for rid in added:
        statuses[rid] = await _store_status(provider, rid, listing_cache(request.app))
    return web.json_response({"offers": offers, "added": added, "status": statuses})


async def handle_market_registries_put(request: web.Request) -> web.Response:
    """PUT /api/apps/market/registries -- ``{"added": [<id>, ...]}`` replaces the added ids."""
    from kiro_crew.dashboard.handlers._shared import body_not_object_error

    denied = await _store_gate(request, "market_registries.update")
    if denied is not None:
        return denied
    provider = provider_of(request.app)
    if provider is None:
        return _store_unavailable()
    try:
        body = await request.json()
    except (LookupError, RecursionError, ValueError):
        return _store_error(400, "invalid_json", "invalid JSON")
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    ids = body.get("added")
    if not isinstance(ids, list) or len(ids) > MAX_ADDED:
        return _store_error(400, "market_registry_invalid", "added must be a list of store ids")
    offered_ids = {o["id"] for o in offered(provider)}
    wanted: list[str] = []
    for rid in ids:
        if not isinstance(rid, str) or rid not in offered_ids:
            _store_audit("market_registries.update", "denied", "unknown store")
            return _store_error(
                400, "market_registry_unknown", "that Kiro Space store is not offered"
            )
        if rid not in wanted:
            wanted.append(rid)

    from kiro_crew.config.loader import config_path
    from kiro_crew.dashboard.chat_utils import run_config_write

    before = await _store_added_ids()
    try:
        written = await run_config_write(
            write_added_ids, Path(config_path()), wanted, frozenset(offered_ids)
        )
    except Exception as exc:  # noqa: BLE001 - report, never 500 with detail
        logger.warning("Writing market_registries failed: %s", type(exc).__name__)
        _store_audit("market_registries.update", "failed", "config write")
        return _store_error(
            500, "market_registries_config_write_failed", "could not save the stores"
        )
    cache = listing_cache(request.app)
    for rid in set(before) - set(written):
        cache.clear(rid)
    _store_audit("market_registries.update", "success", f"added={','.join(wanted)}")
    return web.json_response({"ok": True, "added": wanted})


async def _store_sign(request: web.Request, action: str) -> web.Response:
    operation = f"market_registries.{action}"
    denied = await _store_gate(request, operation)
    if denied is not None:
        return denied
    usable = await _store_usable(request, request.match_info.get("id"))
    if isinstance(usable, web.Response):
        return usable
    provider, rid = usable
    # The body is empty: no flow, no option the provider would act on.
    if request.can_read_body:
        try:
            body = await request.json()
        except (LookupError, RecursionError, ValueError):
            return _store_error(400, "invalid_json", "invalid JSON")
        if body != {}:
            return _store_error(400, "market_signin_invalid", "the body must be an empty object")
    method = provider.sign_in if action == "signin" else provider.sign_out
    try:
        view = sign_in_view(await _store_off_loop(method, rid))
    except Exception as exc:  # noqa: BLE001 - the provider's detail stays here
        logger.warning("Kiro Space store %s failed: %s", action, type(exc).__name__)
        _store_audit(operation, "failed", f"registry={rid}")
        return _store_error(502, "market_signin_failed", "Kiro Space sign-in did not start")
    # Both change who is signed in: no rows read before may show after.
    listing_cache(request.app).clear(rid)
    _store_audit(operation, "success", f"registry={rid} state={view.get('state', '')}")
    return web.json_response({"ok": True, "registry": rid, **view})


async def handle_market_registry_signin(request: web.Request) -> web.Response:
    """POST /api/apps/market/registries/{id}/signin -- start the provider's sign-in."""
    return await _store_sign(request, "signin")


async def handle_market_registry_signout(request: web.Request) -> web.Response:
    """POST /api/apps/market/registries/{id}/signout -- sign out; drops cached rows."""
    return await _store_sign(request, "signout")


def _store_client(provider: Any, rid: str) -> Any:
    """The provider's client; ``None`` when it reports the store is signed out."""
    try:
        return provider.client(rid)
    except MarketInstallError:
        return None


async def _store_read_failed(
    provider: Any, rid: str, exc: MarketInstallError, operation: str, cache: ListingCache
) -> dict[str, Any]:
    """Answer fields for a failed store read: the code and, on a refusal, a fresh status."""
    _store_audit(operation, "failed", f"registry={rid} code={exc.code}")
    status = await _store_status(provider, rid, cache)
    if exc.code == "market_signed_out" and status["state"] == SIGNED_IN:
        status = {"state": NEEDS_SIGN_IN, "account": ""}
        cache.clear(rid)
    return {"status": status, "error": {"code": exc.code}}


async def _store_moved(
    provider: Any, rid: str, cache: ListingCache, status: dict[str, Any], generation: int
) -> dict[str, Any] | None:
    """Answer fields when the store changed hands during a read; ``None`` when it did not.

    It did when a clear ran (a sign-in, a sign-out, a sign-in-needing status, a
    new account) or the status read now names another account than *status*.
    shortcut: a switch to another account and back within one read is not
    seen; closing it needs the provider to name the account its client holds.
    """
    fresh = await _store_status(provider, rid, cache)
    if (
        cache.generation(rid) == generation
        and fresh["state"] == SIGNED_IN
        and fresh["account"] == status["account"]
    ):
        return None
    moved: dict[str, Any] = {"status": fresh}
    if fresh["state"] not in (SIGNED_IN, NEEDS_SIGN_IN, NO_ACCESS):
        moved["error"] = {"code": "market_unavailable"}
    return moved


async def _store_installed() -> dict[str, dict[str, Any]]:
    from kiro_crew.apps.manager import list_apps

    return installed_by_name(await asyncio.to_thread(list_apps))


async def handle_market_listings(request: web.Request) -> web.Response:
    """GET /api/apps/market/listings?registry=<id>[&refresh=1] -- the store's apps as rows.

    Always 200 once the store is usable. Rows come only under a ``signed_in``
    status, from the cache entry of that status's account, and ``account``
    names it (``""`` with no rows). ``needs_sign_in`` or ``no_access`` answers
    no rows and the status, so the page shows a sign-in card. Any other status
    (``unknown``) answers no rows, the status and ``error.code``. A read that
    ends after the store changed hands (:func:`_store_moved`) caches nothing
    and answers no rows and the fresh status. A failed read answers the error
    code and a fresh status, with this account's saved rows only while that
    status still names it. ``truncated`` is true when more than ``MAX_PAGES``
    pages exist.
    """
    denied = await _store_gate(request, "market_listings.read")
    if denied is not None:
        return denied
    usable = await _store_usable(request, request.query.get("registry"))
    if isinstance(usable, web.Response):
        return usable
    provider, rid = usable
    cache = listing_cache(request.app)
    status = await _store_status(provider, rid, cache)
    base: dict[str, Any] = {
        "ok": True,
        "registry": rid,
        "account": "",
        "rows": [],
        "truncated": False,
    }
    if status["state"] in (NEEDS_SIGN_IN, NO_ACCESS):
        return web.json_response({**base, "status": status})
    if status["state"] != SIGNED_IN:
        # The store or its CLI cannot be read now, so nobody is known to be
        # signed in: no saved rows, whoever's they were.
        return web.json_response(
            {**base, "status": status, "error": {"code": "market_unavailable"}}
        )
    failure: dict[str, Any] = {}
    generation = cache.generation(rid)
    cached = cache.get(rid, status["account"])
    # A refresh reads again, but the old rows stay until a new read succeeds.
    if cached is None or request.query.get("refresh") == "1":
        client = _store_client(provider, rid)
        if client is None:
            cache.clear(rid)
            return web.json_response({**base, "status": {"state": NEEDS_SIGN_IN, "account": ""}})
        try:
            items, truncated = await _store_off_loop(fetch_listings, client)
        except MarketInstallError as exc:
            failure = await _store_read_failed(provider, rid, exc, "market_listings.read", cache)
        except Exception as exc:  # noqa: BLE001 - never leak the store's detail
            logger.warning("Kiro Space listing read failed: %s", type(exc).__name__)
            fresh_status = await _store_status(provider, rid, cache)
            failure = {"status": fresh_status, "error": {"code": "market_unavailable"}}
        else:
            moved = await _store_moved(provider, rid, cache, status, generation)
            if moved is not None:
                return web.json_response({**base, **moved})
            cache.put(rid, status["account"], items, truncated)
            cached = cache.get(rid, status["account"])
        if failure:
            fresh = failure["status"]
            if fresh["state"] in (NEEDS_SIGN_IN, NO_ACCESS):
                return web.json_response({**base, "status": fresh})
            # Saved rows only while the store is still this account's.
            same = (
                cache.generation(rid) == generation
                and fresh["state"] == SIGNED_IN
                and fresh["account"] == status["account"]
            )
            cached = cache.get(rid, status["account"]) if same else None
            if cached is None:
                return web.json_response({**base, **failure})
    installed = await _store_installed()
    rows = []
    for view in cached.items if cached is not None else []:
        row = listing_row(view, rid, installed)
        if row is not None:
            rows.append(row)
    return web.json_response(
        {
            **base,
            "status": status,
            "account": status["account"],
            "rows": rows,
            "truncated": cached.truncated if cached is not None else False,
            **failure,
        }
    )


async def handle_market_listing(request: web.Request) -> web.Response:
    """GET /api/apps/market/listing?registry=<id>&ref=<ns>/<name> -- one listing, uncached.

    ``listing`` only under ``signed_in``, with ``account`` naming whose it is
    (``""`` without one); a read that ends after the store changed hands
    (:func:`_store_moved`) answers no listing and the fresh status.
    """
    denied = await _store_gate(request, "market_listing.read")
    if denied is not None:
        return denied
    ref = request.query.get("ref", "")
    if not LISTING_REF_RE.fullmatch(ref):
        return _store_error(400, "market_resolve_invalid", "listing ref is invalid")
    usable = await _store_usable(request, request.query.get("registry"))
    if isinstance(usable, web.Response):
        return usable
    provider, rid = usable
    cache = listing_cache(request.app)
    status = await _store_status(provider, rid, cache)
    base: dict[str, Any] = {"ok": True, "registry": rid, "account": "", "listing": None}
    if status["state"] != SIGNED_IN:
        return web.json_response({**base, "status": status})
    generation = cache.generation(rid)
    client = _store_client(provider, rid)
    if client is None:
        cache.clear(rid)
        return web.json_response({**base, "status": {"state": NEEDS_SIGN_IN, "account": ""}})
    try:
        view = await _store_off_loop(client.get_listing, ref)
    except MarketInstallError as exc:
        failed = await _store_read_failed(provider, rid, exc, "market_listing.read", cache)
        return web.json_response({**base, **failed})
    except Exception as exc:  # noqa: BLE001 - never leak the store's detail
        logger.warning("Kiro Space listing detail failed: %s", type(exc).__name__)
        fresh_status = await _store_status(provider, rid, cache)
        return web.json_response(
            {**base, "status": fresh_status, "error": {"code": "market_unavailable"}}
        )
    moved = await _store_moved(provider, rid, cache, status, generation)
    if moved is not None:
        return web.json_response({**base, **moved})
    row = listing_row(view, rid, await _store_installed(), detail=True)
    if row is None or row["ref"] != ref:
        return web.json_response(
            {**base, "status": status, "error": {"code": "market_resolve_invalid"}}
        )
    return web.json_response(
        {**base, "status": status, "account": status["account"], "listing": row}
    )


def register_market_client_factory(app: web.Application) -> None:
    """Set ``app["market_client_factory"]`` at startup when a provider is present.

    At startup, not now: core registers its routes before a build contributes
    its own (which is when ``app["market_registries"]`` is set), and the app is
    frozen only after the startup hooks run.
    """

    async def _set_factory(app: web.Application) -> None:
        if provider_of(app) is not None and app.get(MARKET_CLIENT_FACTORY_KEY) is None:
            app[MARKET_CLIENT_FACTORY_KEY] = make_market_client_factory(app)

    app.on_startup.append(_set_factory)


def register_market_registry_routes(app: web.Application) -> None:
    """Add the store routes, the listing cache and the factory hook.

    Call before the ``{name}`` catch-all, and before the app freezes.
    """
    app[MARKET_LISTING_CACHE_KEY] = ListingCache()
    register_market_client_factory(app)
    app.router.add_get("/api/apps/market/registries", handle_market_registries_get)
    app.router.add_put("/api/apps/market/registries", handle_market_registries_put)
    app.router.add_post("/api/apps/market/registries/{id}/signin", handle_market_registry_signin)
    app.router.add_post("/api/apps/market/registries/{id}/signout", handle_market_registry_signout)
    app.router.add_get("/api/apps/market/listings", handle_market_listings)
    app.router.add_get("/api/apps/market/listing", handle_market_listing)
