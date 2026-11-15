"""Dreaming's review queue and the member ``SOUL.md`` read behind the memory app host.

Accepting a Dreaming proposal re-reads every row it cites and refuses a stale one without
taking it off the list; the queue serves Dreaming's proposals only. ``SOUL.md`` is read
through the essentials builder's contained, no-follow, size-capped read and redacted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from conftest import make_escaping_link, requires_symlinks
from kiro_crew import memory_dream, memory_edit
from kiro_crew import memory_kind_routing as kind_log
from kiro_crew.dashboard.handlers import memory_app_host as host
from kiro_crew.dashboard.handlers._shared import _redact_memory_field
from kiro_crew.vector_memory import VectorMemoryStore

_STAMP = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).isoformat()


@pytest.fixture
def store(tmp_path):
    opened = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=4)
    opened.init()
    yield opened
    opened.close()


def _lesson(store: VectorMemoryStore, key: str, rule: str) -> None:
    with store._db_lock:
        store.db.execute(
            "INSERT INTO semantic_memory (key, value_json, confidence, source, created_at, "
            "updated_at, is_deleted) VALUES (?, ?, 0.5, 'consolidation', ?, ?, 0)",
            (key, json.dumps({"rule": rule, "applies": "on_topic"}), _STAMP, _STAMP),
        )
        store.db.commit()


def _forget_row(store: VectorMemoryStore, key: str) -> None:
    with store._db_lock:
        store.db.execute("UPDATE semantic_memory SET is_deleted = 1 WHERE key = ?", (key,))
        store.db.commit()


def _propose(store: VectorMemoryStore, entry: dict) -> str:
    with store._db_lock:
        kind_log._upsert(store.db, memory_dream.PROPOSALS_KEY, json.dumps([entry]), _STAMP)
        store.db.commit()
    return host._dream_id(entry)


def _listed(store: VectorMemoryStore) -> list[dict]:
    with store._db_lock:
        return memory_dream.read_proposals(store.db)


_MERGE = {
    "run": "r1",
    "kind": "near_duplicate",
    "keys": ["lesson.keep", "lesson.copy"],
    "changes": ["lesson.copy"],
    "gate": "pass",
    "seen": {"lesson.keep": _STAMP, "lesson.copy": _STAMP},
}


def _correct_row(store: VectorMemoryStore, key: str, rule: str) -> None:
    later = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc).isoformat()
    with store._db_lock:
        store.db.execute(
            "UPDATE semantic_memory SET value_json = ?, updated_at = ? WHERE key = ?",
            (json.dumps({"rule": rule, "applies": "on_topic"}), later, key),
        )
        store.db.commit()


@pytest.fixture
def edits(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(host, "_edit", lambda tier, s, k, ids, op: seen.append((ids, op)) or 1)
    return seen


def test_accepting_a_merge_whose_keeper_is_gone_is_refused_and_stays_listed(store, edits):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)
    _forget_row(store, "lesson.keep")

    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_stale" and refused.value.status == 409
    assert edits == []
    assert [host._dream_id(p) for p in _listed(store)] == [dream_id]


def test_accepting_a_merge_whose_folded_row_was_corrected_is_refused(store, edits):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)
    _correct_row(store, "lesson.copy", "Run the tests first, then lint.")

    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_stale" and edits == []
    assert [host._dream_id(p) for p in _listed(store)] == [dream_id]


def test_a_proposal_without_its_proposal_time_rows_cannot_be_accepted(store, edits):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, {k: v for k, v in _MERGE.items() if k != "seen"})

    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_stale" and edits == []


def test_rejecting_a_stale_proposal_takes_it_off_the_list(store, edits):
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)

    assert host._resolve(store, "", b"k", dream_id, "reject") == "rejected"
    assert edits == [] and _listed(store) == []


def test_accepting_a_live_merge_forgets_only_the_folded_row(store, edits):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)

    assert host._resolve(store, "", b"k", dream_id, "accept") == "accepted"
    assert edits == [([("directive", "lesson.copy")], {"type": "forget"})]
    assert _listed(store) == []


@pytest.mark.parametrize("kind", ["uncertain_merge", "legacy_tier"])
def test_accepting_a_string_lesson_rewrite_is_refused_and_stays_listed(store, edits, kind):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    with store._db_lock:
        store.db.execute(
            "UPDATE semantic_memory SET value_json = ? WHERE key = ?",
            (json.dumps("Run the tests."), "lesson.keep"),
        )
        store.db.commit()
        before = host._live_semantic(store.db, _MERGE["keys"])
    entry = dict(_MERGE, kind=kind, text="Run the tests first, then lint.")
    if kind == "legacy_tier":
        entry["changes"] = ["lesson.copy", "lesson.keep"]
    dream_id = _propose(store, entry)

    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", dream_id, "accept")

    assert refused.value.code == "review_item_unsupported"
    assert refused.value.status == 409
    assert edits == []
    with store._db_lock:
        assert host._live_semantic(store.db, _MERGE["keys"]) == before
    assert _listed(store) == [entry]
    assert [item["id"] for item in host._dream_items(store)] == [dream_id]
    assert host._resolve(store, "", b"k", dream_id, "reject") == "rejected"
    assert edits == [] and _listed(store) == []


def test_an_edit_that_fails_leaves_the_proposal_listed(store, monkeypatch):
    _lesson(store, "lesson.keep", "Run the tests.")
    _lesson(store, "lesson.copy", "Run the tests first.")
    dream_id = _propose(store, _MERGE)

    def refuse(*_args, **_kwargs):
        raise memory_edit.MemoryEditError("Stale preview.", "stale_preview", 409)

    monkeypatch.setattr(host, "_edit", refuse)
    with pytest.raises(memory_edit.MemoryEditError):
        host._resolve(store, "", b"k", dream_id, "accept")

    assert [host._dream_id(p) for p in _listed(store)] == [dream_id]


@pytest.mark.parametrize("item_id", ["conflict:1", "replaced:ep1", "dream:missing"])
def test_only_a_listed_dreaming_proposal_resolves(store, edits, item_id):
    with pytest.raises(memory_edit.MemoryEditError) as refused:
        host._resolve(store, "", b"k", item_id, "accept")
    assert refused.value.status == 404 and edits == []


# ── SOUL.md ──────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A configured member workspace: the essentials read admits a ``SOUL.md`` in it."""
    from kiro_crew import member_essential_context as essentials

    real_table = essentials._managed_root_table

    def table(**kwargs):
        roots, workspaces, resolved = real_table(**kwargs)
        return roots, [*workspaces, tmp_path], {**resolved, tmp_path: tmp_path.resolve()}

    monkeypatch.setattr(essentials, "_managed_root_table", table)
    return tmp_path


def test_a_missing_soul_reads_as_empty(workspace):
    assert host._read_soul(workspace / "SOUL.md", workspace) == ""


@pytest.mark.parametrize("target", ["outside", pytest.param("/dev/zero", marks=requires_symlinks)])
def test_a_soul_link_leaving_the_workspace_is_refused(workspace, tmp_path_factory, target):
    if target == "outside":
        outside = tmp_path_factory.mktemp("elsewhere")
        (outside / "secret.py").write_text("not for the app", encoding="utf-8")
        path = workspace / make_escaping_link(workspace, outside)
    else:
        path = workspace / "SOUL.md"
        path.symlink_to(target)

    with pytest.raises(ValueError):
        host._read_soul(path, workspace)


def test_a_soul_over_the_cap_is_refused(workspace):
    (workspace / "SOUL.md").write_text("x" * (host.SOUL_MAX_CHARS + 1), encoding="utf-8")
    with pytest.raises(host._SoulTooLarge):
        host._read_soul(workspace / "SOUL.md", workspace)


@pytest.mark.asyncio
async def test_the_soul_read_response_is_redacted(workspace, monkeypatch):
    secret = "ghp_" + "a" * 36
    assert _redact_memory_field(secret) != secret  # the precondition this test needs
    (workspace / "SOUL.md").write_text(f"Be kind.\n{secret}\n", encoding="utf-8")

    async def owner(request, operation):
        return None

    monkeypatch.setattr(host, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(host, "_member_workspace_dir", lambda slug: ("Ada", workspace))
    app = web.Application()
    app["state"] = SimpleNamespace()
    request = make_mocked_request(
        "GET", "/api/members/ada/soul", app=app, match_info={"slug": "ada"}
    )

    response = await host.api_member_soul(request)

    assert isinstance(response, web.Response) and response.text is not None
    body = json.loads(response.text)
    assert response.status == 200 and body["member"] == "Ada"
    assert secret not in body["text"] and body["text"].startswith("Be kind.")


@pytest.mark.asyncio
async def test_a_soul_holding_a_secret_is_not_replaced_by_its_masked_copy(workspace, monkeypatch):
    secret = "ghp_" + "b" * 36
    original = f"Be kind.\n{secret}\n"
    (workspace / "SOUL.md").write_text(original, encoding="utf-8")

    async def owner(request, operation):
        return None

    async def body(request, max_bytes):
        return {"text": _redact_memory_field(original)}, None

    monkeypatch.setattr(host, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(host, "read_bounded_json", body)
    monkeypatch.setattr(host, "_member_workspace_dir", lambda slug: ("Ada", workspace))
    app = web.Application()
    app["state"] = SimpleNamespace()
    request = make_mocked_request(
        "PUT", "/api/members/ada/soul", app=app, match_info={"slug": "ada"}
    )

    response = await host.api_member_soul(request)

    assert response.status == 409
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_a_soul_no_editor_could_show_is_not_overwritten(workspace, monkeypatch):
    # Over the editor's cap, so its GET answers 413: a save must not count it as empty.
    original = "x" * (host.SOUL_MAX_CHARS + 1)
    (workspace / "SOUL.md").write_text(original, encoding="utf-8")

    async def owner(request, operation):
        return None

    async def body(request, max_bytes):
        return {"text": "Be kind."}, None

    monkeypatch.setattr(host, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(host, "read_bounded_json", body)
    monkeypatch.setattr(host, "_member_workspace_dir", lambda slug: ("Ada", workspace))
    app = web.Application()
    app["state"] = SimpleNamespace()
    request = make_mocked_request(
        "PUT", "/api/members/ada/soul", app=app, match_info={"slug": "ada"}
    )

    response = await host.api_member_soul(request)

    assert isinstance(response, web.Response) and response.status == 409
    assert json.loads(response.text or "{}")["code"] == "document_unchecked"
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == original
