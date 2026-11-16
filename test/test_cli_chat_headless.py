"""``kirocrew chat --headless``: flags, conductor-agent resolution, the run loop and exit codes.

The run loop is driven against a scripted gateway client, so every way a run ends is
pinned without a gateway; the end-to-end run against a real gateway and the fake ACP
backend is in ``test_e2e_smoke.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace

import pytest

from kiro_crew import cli_chat_headless as h
from kiro_crew import crew_mode


def _parse(argv):
    from kiro_crew.cli_parser_agent import register_chat_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register_chat_parser(sub, argparse.ArgumentParser(add_help=False))
    return parser.parse_args(["chat", *argv])


# ---------- flags ----------
def test_parser_accepts_every_headless_flag(tmp_path):
    a = _parse(
        [
            "--headless",
            "-m",
            "go",
            "--mode",
            "conductor",
            "--approval-mode",
            "trust",
            "--until-done",
            "--idle-grace",
            "5",
            "--cwd",
            str(tmp_path),
            "--timeout",
            "60",
            "--budget",
            "12.5",
            "--json",
        ]
    )
    o = h.options_from_args(a)
    assert (o.mode, o.approval_mode, o.until_done, o.idle_grace) == (
        "conductor",
        "trust",
        True,
        5.0,
    )
    assert (o.cwd, o.timeout, o.budget, o.json) == (str(tmp_path), 60.0, 12.5, True)


def test_approval_mode_offers_only_trust():
    with pytest.raises(SystemExit):
        _parse(["--headless", "-m", "x", "--approval-mode", "yolo"])


def test_message_file_is_read(tmp_path):
    f = tmp_path / "seed.txt"
    f.write_text("from a file", encoding="utf-8")
    assert (
        h.options_from_args(_parse(["--headless", "--message-file", str(f)])).message
        == "from a file"
    )


@pytest.mark.parametrize(
    "argv, why",
    [
        (["--headless"], "needs a seed"),
        (["--headless", "-m", "x", "--mode", "conductor", "--agent", "kirocrew"], "--agent cannot"),
        (["--headless", "-m", "x", "--cwd", "relative/dir"], "--cwd must be"),
        (["--headless", "-m", "x", "--timeout", "-1"], "--timeout must not"),
        (["--headless", "-m", "x", "--budget", "-2"], "--budget must not"),
    ],
)
def test_bad_flag_sets_are_usage_errors(argv, why):
    with pytest.raises(h.UsageError, match=why):
        h.options_from_args(_parse(argv))


def test_headless_flags_need_headless():
    assert (
        h.refuse_headless_flags_without_headless(_parse(["-m", "x", "--until-done"]))
        == "--until-done"
    )
    assert h.refuse_headless_flags_without_headless(_parse(["-m", "x", "--json"])) == "--json"
    assert h.refuse_headless_flags_without_headless(_parse(["-m", "x"])) == ""


def test_main_usage_error_exits_2(capsys):
    assert h.main(_parse(["--headless"])) == h.EXIT_USAGE


# ---------- conductor agent ----------
def _cfg(value):
    return SimpleNamespace(agent=SimpleNamespace(crew_mode_agent=value))


def test_conductor_agent_default_when_unset():
    got = h.resolve_conductor_agent(_cfg(""))
    assert got == {
        "name": crew_mode.DEFAULT_CREW_MODE_AGENT,
        "source": "default",
        "configured": crew_mode.DEFAULT_CREW_MODE_AGENT,
    }


def test_conductor_agent_from_config():
    got = h.resolve_conductor_agent(_cfg("kirocrew-ledger-conductor"))
    assert got["name"] == "kirocrew-ledger-conductor" and got["source"] == "config"


def test_conductor_agent_fallback_when_unusable(monkeypatch):
    monkeypatch.setattr(crew_mode, "_unusable", {"no-such-agent": "kirocrew-conductor"})
    got = h.resolve_conductor_agent(_cfg("no-such-agent"))
    assert got == {
        "name": "kirocrew-conductor",
        "source": "fallback",
        "configured": "no-such-agent",
    }


# ---------- reading the run ----------
def test_transcript_credits_sums_turn_stats(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"role": "assistant", "meta": {"turn_stats": {"credits": 1.5}}},
                {"role": "assistant", "meta": {"turn_stats": {"credits": 2}}},
                {"role": "assistant", "meta": {"turn_stats": {"credits": True}}},
                {"role": "user"},
            ]
        )
        + "\nnot json\n",
        encoding="utf-8",
    )
    assert h.transcript_credits(p) == 3.5
    assert h.transcript_credits(tmp_path / "missing.jsonl") == 0.0


def test_classify_failure():
    assert (
        h.classify_failure("KAS runs through kiro-cli's relay. Not signed in to Kiro.")
        == "not_signed_in"
    )
    assert h.classify_failure("The sandbox is unavailable on this host") == "sandbox_refused"
    assert h.classify_failure("model unavailable") == "error"


def test_exit_codes_are_distinct():
    assert len(set(h.STATUS_EXIT.values())) == len(h.STATUS_EXIT)
    assert h.STATUS_EXIT["done"] == 0 and h.STATUS_EXIT["asked"] == 3


# ---------- the run loop ----------
ROOT = "chat-1-1800000000"
WORKER = "chat-2-1800000001"


class FakeGateway:
    """Answers the run's requests from a script of per-poll states.

    Each state: ``row`` (the root's slot row), ``worker`` (a worker's row, listed only
    with ``crew=all``, as the owner's slot list does), ``text`` / ``role`` (the root's
    last turn) and ``cards`` (pending question cards).
    """

    def __init__(self, states, *, slot_extra=None, catalog=None):
        self.states = list(states)
        self.calls = []
        self.slot_extra = slot_extra or {}
        self.catalog = catalog or {}
        self.i = -1

    def sign_in(self):
        self.calls.append(("SIGN_IN", "", None))

    def call(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        if path == "/api/agents/catalog":
            return self.catalog
        if path == "/api/chat/slots" and method == "POST":
            return {"key": ROOT, "agent": (body or {}).get("agent", "kirocrew"), **self.slot_extra}
        if path == "/api/chat/folders":
            return {"id": "f1"}
        if path.startswith("/api/chat/slots?") and method == "GET":
            self.i = min(self.i + 1, len(self.states) - 1)
            state = self.states[self.i]
            rows = [dict(state.get("row", {}), key=ROOT)]
            if "worker" in state and "crew=all" in path:
                rows.append(dict(state["worker"], key=WORKER))
            return rows
        if path == f"/api/chat/slots/{ROOT}" and method == "GET":
            state = self.states[self.i]
            text = state.get("text", "")
            turn = [{"role": state.get("role", "assistant"), "content": text}] if text else []
            return {"messages": [{"role": "user", "content": "go"}] + turn}
        if path == "/api/ask-question/pending":
            return {"pending": self.states[self.i].get("cards", [])}
        return {}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 1.0)


def _item(state, worker=None, **extra):
    """One board row, as ``GET /api/crew-board`` returns it."""
    return {
        "item_id": "it_00000001",
        "title": "t",
        "state": state,
        "worker_session_key": worker,
        **extra,
    }


def _opts(caps=None, **kw):
    """Options; the duration caps come in one dict, applied as attributes."""
    o = h.Options(message="go", **kw)
    for k, v in (caps or {}).items():
        o.__dict__[k] = v
    return o


def _run(opts, gw, monkeypatch, *, items=None, credits=0.0, cfg=None):
    monkeypatch.setattr(
        h, "ledger_items", lambda client, slot: items() if callable(items) else (items or [])
    )
    monkeypatch.setattr(h, "transcript_credits", lambda p: credits)
    monkeypatch.setattr(
        h, "transcript_path", lambda slot: __import__("pathlib").Path(f"/x/dashboard_{slot}.jsonl")
    )
    clock = Clock()
    return h.run(opts, gw, cfg or _cfg(""), clock=clock, sleep=clock.sleep, poll_secs=1.0)


BUSY = {"row": {"running": True}}
REPLY = {"row": {}, "text": "done, pong"}


def test_single_turn_done(monkeypatch, capsys):
    gw = FakeGateway([BUSY, REPLY])
    r = _run(_opts(), gw, monkeypatch)
    assert (r.status, r.completed, r.asks) == ("done", True, 0)
    assert (
        "POST",
        "/api/chat?ws=1",
        {"message": "go", "slot": ROOT, "agent": "kirocrew"},
    ) in gw.calls
    assert not any(c[1] == "/api/chat/mode" for c in gw.calls)
    assert r.to_json()["exit_code"] == 0 and r.transcript.endswith(f"dashboard_{ROOT}.jsonl")
    assert "headless: poll=2 busy=0" in capsys.readouterr().err


def test_approval_mode_trust_sets_trust_on_own_slot_only(monkeypatch):
    gw = FakeGateway([REPLY])
    r = _run(_opts(approval_mode="trust"), gw, monkeypatch)
    modes = [c for c in gw.calls if c[1] == "/api/chat/mode"]
    assert modes == [
        ("POST", "/api/chat/mode", {"slot": ROOT, "mode": "trust"}),
        ("POST", "/api/chat/mode", {"slot": ROOT, "mode": "normal"}),
    ]
    assert r.approval_mode == "trust"


def test_a_clean_done_disarms_automation_too(monkeypatch):
    class Armed(FakeGateway):
        def call(self, method, path, body=None, timeout=60):
            if path == f"/api/autonudge/slot/{ROOT}":
                self.calls.append((method, path, body))
                return {"enabled": True, "loop": {"id": "loop-9"}}
            return super().call(method, path, body, timeout)

    gw = Armed([REPLY])
    r = _run(_opts(), gw, monkeypatch)
    assert (r.status, r.completed) == ("done", True)
    assert ("DELETE", "/api/autonudge/loop-9", None) in gw.calls


def test_a_refused_seed_is_not_done(monkeypatch):
    gw = FakeGateway(
        [{"row": {}, "role": "system", "text": "🔒 Prompt blocked — size limit (64KB)."}]
    )
    r = _run(_opts(), gw, monkeypatch)
    assert (r.status, r.completed, r.to_json()["exit_code"]) == ("error", False, h.EXIT_ERROR)


def test_cwd_opens_a_project_folder(monkeypatch, tmp_path):
    gw = FakeGateway([REPLY])
    _run(_opts(cwd=str(tmp_path)), gw, monkeypatch)
    assert (
        "POST",
        "/api/chat/folders",
        {"name": f"headless {tmp_path.name}", "project_dir": str(tmp_path)},
    ) in gw.calls
    create = next(c for c in gw.calls if c[:2] == ("POST", "/api/chat/slots"))
    assert create[2]["folder_id"] == "f1"


def test_conductor_mode_opens_on_the_gateway_resolved_agent(monkeypatch):
    gw = FakeGateway(
        [REPLY],
        slot_extra={"crew_mode_agent": "kirocrew-ledger-conductor"},
        catalog={"crew_mode_agent": "kirocrew-ledger-conductor"},
    )
    r = _run(_opts(mode="conductor"), gw, monkeypatch, cfg=_cfg("kirocrew-ledger-conductor"))
    create = next(c for c in gw.calls if c[:2] == ("POST", "/api/chat/slots"))
    assert create[2]["agent"] == "kirocrew-ledger-conductor"
    assert create[2]["agent_kind"] == "template"
    assert r.agent == {
        "name": "kirocrew-ledger-conductor",
        "source": "config",
        "configured": "kirocrew-ledger-conductor",
    }


def test_conductor_mode_takes_the_gateway_fallback(monkeypatch):
    # The configured name is unusable; only the gateway knows the conductor in force.
    gw = FakeGateway(
        [REPLY],
        slot_extra={"crew_mode_agent": "kirocrew-conductor"},
        catalog={"crew_mode_agent": "kirocrew-conductor"},
    )
    r = _run(_opts(mode="conductor"), gw, monkeypatch, cfg=_cfg("no-such-agent"))
    create = next(c for c in gw.calls if c[:2] == ("POST", "/api/chat/slots"))
    assert create[2]["agent"] == "kirocrew-conductor"
    assert r.agent == {
        "name": "kirocrew-conductor",
        "source": "fallback",
        "configured": "no-such-agent",
    }


def test_conductor_mode_refuses_a_slot_not_stamped(monkeypatch):
    gw = FakeGateway([REPLY])  # no crew_mode_agent on the created slot
    r = _run(_opts(mode="conductor"), gw, monkeypatch)
    assert r.status == "error" and "conductor mode" in r.error


def test_until_done_waits_for_every_item(monkeypatch):
    seq = iter([[_item("open")], [_item("open")], [_item("accepted")]])
    last = [[]]

    def items():
        last[0] = next(seq, last[0])
        return last[0]

    gw = FakeGateway([REPLY] * 4, slot_extra={"crew_mode_agent": "kirocrew-conductor"})
    r = _run(
        _opts(mode="conductor", until_done=True, caps={"idle_grace": 100}),
        gw,
        monkeypatch,
        items=items,
    )
    assert (r.status, r.completed) == ("done", True)
    assert r.items == {"total": 1, "terminal": 1, "open": 0}


def test_until_done_survives_a_wake(monkeypatch):
    # idle, then a patrol wake makes it busy again, then idle past the grace window
    gw = FakeGateway([REPLY, BUSY, REPLY, REPLY, REPLY, REPLY])
    r = _run(_opts(until_done=True, caps={"idle_grace": 2}), gw, monkeypatch)
    assert r.status == "done" and gw.i >= 4


def test_a_busy_worker_keeps_the_run_alive(monkeypatch):
    # The worker row only shows with crew=all; while it is busy the run is not done.
    states = [dict(REPLY, worker={"running": True})] * 3 + [dict(REPLY, worker={})]
    gw = FakeGateway(states, slot_extra={"crew_mode_agent": "kirocrew-conductor"})
    seq = iter(
        [[_item("open", f"dashboard:{WORKER}")]] * 3 + [[_item("accepted", f"dashboard:{WORKER}")]]
    )
    r = _run(
        _opts(mode="conductor", until_done=True, caps={"idle_grace": 100}),
        gw,
        monkeypatch,
        items=lambda: next(seq),
    )
    assert r.status == "done" and gw.i == 3
    assert all(
        "crew=all" in c[1]
        for c in gw.calls
        if c[0] == "GET" and c[1].startswith("/api/chat/slots?")
    )


def test_a_worker_approval_is_an_ask(monkeypatch):
    gw = FakeGateway(
        [dict(BUSY, worker={"pending_approval": True, "pending_approval_info": {"tool": "shell"}})],
        slot_extra={"crew_mode_agent": "kirocrew-conductor"},
    )
    r = _run(_opts(mode="conductor"), gw, monkeypatch, items=[_item("open", f"dashboard:{WORKER}")])
    assert r.status == "asked"
    assert r.ask_events[0] | {"t_s": ""} == {
        "kind": "approval",
        "slot": WORKER,
        "text": "shell",
        "t_s": "",
    }
    stops = {c[1] for c in gw.calls if c[1].endswith("/stop")}
    assert stops == {f"/api/chat/slots/{ROOT}/stop", f"/api/chat/slots/{WORKER}/stop"}


def test_ask_by_options_list_ends_the_run(monkeypatch):
    gw = FakeGateway([{"row": {}, "text": "Pick one [OPTIONS: A | B]"}])
    r = _run(_opts(), gw, monkeypatch)
    assert (r.status, r.asks, r.ask_events[0]["kind"]) == ("asked", 1, "options")
    assert r.to_json()["exit_code"] == h.EXIT_ASKED
    assert ("POST", f"/api/chat/slots/{ROOT}/stop", {}) in gw.calls


def test_ask_by_question_card(monkeypatch):
    card = {"slot": ROOT, "questions": [{"question": "which?"}]}
    gw = FakeGateway([{"row": {"running": True}, "cards": [card]}])
    r = _run(_opts(), gw, monkeypatch)
    assert r.status == "asked" and r.ask_events[0]["text"] == "which?"


def test_timeout(monkeypatch):
    gw = FakeGateway([BUSY])
    r = _run(_opts(caps={"timeout": 3}), gw, monkeypatch)
    assert r.status == "timeout" and r.to_json()["exit_code"] == h.EXIT_TIMEOUT


def test_budget(monkeypatch):
    gw = FakeGateway([BUSY])
    r = _run(_opts(caps={"budget": 5}), gw, monkeypatch, credits=6.0)
    assert r.status == "budget" and r.credits == 6.0 and r.to_json()["exit_code"] == h.EXIT_BUDGET


def test_an_error_row_is_a_failure_not_done(monkeypatch):
    gw = FakeGateway([{"row": {}, "role": "error", "text": "model unavailable"}])
    r = _run(_opts(), gw, monkeypatch)
    assert (r.status, r.completed, r.to_json()["exit_code"]) == ("error", False, h.EXIT_ERROR)


def test_not_signed_in_only_from_an_error_row(monkeypatch):
    gw = FakeGateway([{"row": {}, "role": "error", "text": "Not signed in to Kiro."}])
    assert _run(_opts(), gw, monkeypatch).status == "not_signed_in"
    gw = FakeGateway([{"row": {}, "text": "The docs say: not signed in means log in."}])
    assert _run(_opts(), gw, monkeypatch).status == "done"


def test_interrupt_stops_the_run_chats(monkeypatch):
    class Interrupting(FakeGateway):
        def call(self, method, path, body=None, timeout=60):
            if path.startswith("/api/chat/slots?"):
                raise KeyboardInterrupt
            return super().call(method, path, body, timeout)

    gw = Interrupting([BUSY])
    r = _run(_opts(), gw, monkeypatch)
    assert r.status == "interrupted" and r.slot == ROOT
    assert ("POST", f"/api/chat/slots/{ROOT}/stop", {}) in gw.calls
    assert r.to_json()["exit_code"] == h.EXIT_INTERRUPTED


def test_interrupt_during_the_seed_still_stops(monkeypatch):
    class InterruptSeed(FakeGateway):
        def call(self, method, path, body=None, timeout=60):
            if path == "/api/chat?ws=1":
                raise KeyboardInterrupt
            return super().call(method, path, body, timeout)

    gw = InterruptSeed([BUSY])
    r = _run(_opts(approval_mode="trust"), gw, monkeypatch)
    assert r.status == "interrupted"
    assert ("POST", f"/api/chat/slots/{ROOT}/stop", {}) in gw.calls


@pytest.mark.parametrize(
    "exc, status",
    [
        (RuntimeError("GET: HTTP 502"), "error"),
        (h.NoGateway("lost"), "no_gateway"),
        (ValueError("bad json"), "error"),
    ],
)
def test_a_failed_poll_stops_the_run_chats(monkeypatch, exc, status):
    class Failing(FakeGateway):
        def call(self, method, path, body=None, timeout=60):
            if path == f"/api/chat/slots/{ROOT}" and method == "GET":
                raise exc
            return super().call(method, path, body, timeout)

    gw = Failing([BUSY])
    r = _run(_opts(approval_mode="trust"), gw, monkeypatch)
    assert r.status == status and r.error
    assert ("POST", f"/api/chat/slots/{ROOT}/stop", {}) in gw.calls


def test_stop_disarms_automation_and_forces_a_declined_stop(monkeypatch):
    class Compacting(FakeGateway):
        def call(self, method, path, body=None, timeout=60):
            if path == f"/api/autonudge/slot/{ROOT}":
                self.calls.append((method, path, body))
                return {"enabled": True, "loop": {"id": "loop-1"}}
            if path == f"/api/chat/slots/{ROOT}/stop":
                self.calls.append((method, path, body))
                return {"ok": False, "compacting": True}
            return super().call(method, path, body, timeout)

    gw = Compacting([{"row": {}, "text": "Pick [OPTIONS: A | B]"}])
    _run(_opts(), gw, monkeypatch)
    assert ("DELETE", "/api/autonudge/loop-1", None) in gw.calls
    assert ("POST", f"/api/chat/slots/{ROOT}/stop?force=true", {}) in gw.calls


def test_a_literal_options_mention_is_not_an_ask(monkeypatch):
    gw = FakeGateway([{"row": {}, "text": "The literal [OPTIONS: starts a list."}])
    assert _run(_opts(), gw, monkeypatch).status == "done"


def test_a_ledger_ask_is_an_ask(monkeypatch):
    item = _item("open", ask={"what": "approve the deploy?"}, awaits_person=True)
    gw = FakeGateway([REPLY], slot_extra={"crew_mode_agent": "kirocrew-conductor"})
    r = _run(_opts(mode="conductor", until_done=True), gw, monkeypatch, items=[item])
    assert r.status == "asked"
    assert (
        r.ask_events[0]["kind"] == "needs_human"
        and r.ask_events[0]["text"] == "approve the deploy?"
    )


def test_a_chat_created_child_is_part_of_the_run(monkeypatch):
    # chat mode: a session_create child (gateway-minted lineage) keeps the run busy and is stopped.
    child = {"running": True, "created_by": f"dashboard:{ROOT}", "lineage_minted": True}
    gw = FakeGateway([dict(REPLY, worker=child)])
    r = _run(_opts(caps={"timeout": 2}), gw, monkeypatch)
    assert r.status == "timeout"
    assert ("POST", f"/api/chat/slots/{WORKER}/stop", {}) in gw.calls


def test_unminted_lineage_is_not_trusted():
    rows = {WORKER: {"created_by": ROOT, "lineage_minted": False}}
    assert h.descendant_slots(rows, {ROOT}) == {ROOT}


def test_sandbox_producer_text_maps_to_exit_7():
    text = "Sandbox backend unavailable: No OS-level sandbox backend is available"
    assert h.classify_failure(text) == "sandbox_refused"


def test_main_json_reports_no_gateway(monkeypatch, capsys):
    def boom(self):
        raise h.NoGateway("no gateway secret for port 1")

    monkeypatch.setattr(h.GatewayClient, "sign_in", boom)
    monkeypatch.setattr("kiro_crew.port_resolution.resolve_client_port", lambda port=None: 1)
    code = h.main(_parse(["--headless", "-m", "go", "--json"]))
    out = json.loads(capsys.readouterr().out)
    assert code == h.EXIT_NO_GATEWAY == out["exit_code"] and out["status"] == "no_gateway"
    assert {"asks", "wall_s", "credits", "completed", "transcript", "agent", "mode"} <= set(out)


# ---------- gateway client ----------
class _Resp:
    def __init__(self, body=b"{}", cookies=()):
        self._body = body
        self.headers = SimpleNamespace(get_all=lambda name: list(cookies))

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(monkeypatch, answers, secret="s3cret"):
    import io
    import urllib.error

    monkeypatch.setattr(
        "kiro_crew.config.loader.read_local_secret", lambda port, dial_host=None: secret
    )
    c = h.GatewayClient(4321)
    seen = []

    def fake_open(req, timeout, operation):
        seen.append((req.get_method(), req.full_url, dict(req.header_items())))
        a = answers.pop(0)
        if isinstance(a, int):
            raise urllib.error.HTTPError(req.full_url, a, "x", {}, io.BytesIO(b"denied"))
        if isinstance(a, Exception):
            raise a
        return a

    c._open = fake_open
    return c, seen


def test_client_signs_in_and_sends_the_cookie(monkeypatch):
    c, seen = _client(
        monkeypatch,
        [
            _Resp(b'{"token":"tok"}'),
            _Resp(cookies=["mc_token_4321=sess; Path=/"]),
            _Resp(b'{"ok":1}'),
        ],
    )
    c.sign_in()
    assert c.call("POST", "/api/chat/mode", {"slot": "s", "mode": "trust"}) == {"ok": 1}
    assert (
        seen[0][1].endswith("/api/token/local?ttl=1m") and seen[0][2]["X-local-secret"] == "s3cret"
    )
    assert "token=tok" in seen[1][1]
    method, url, headers = seen[2]
    assert (
        method == "POST"
        and url.endswith("/api/chat/mode")
        and headers["Cookie"] == "mc_token_4321=sess"
    )


def test_client_without_cookie_uses_the_token_and_resigns_on_401(monkeypatch):
    c, seen = _client(
        monkeypatch,
        [_Resp(b'{"token":"t1"}'), _Resp(), 401, _Resp(b'{"token":"t2"}'), _Resp(), _Resp(b"")],
    )
    c.sign_in()
    assert c.call("GET", "/api/chat/slots?x=1") == {}
    assert "?x=1&token=t1" in seen[2][1] and "token=t2" in seen[5][1]


def test_client_errors(monkeypatch):
    c, _ = _client(monkeypatch, [403])
    with pytest.raises(RuntimeError, match="refused sign-in: HTTP 403"):
        c.sign_in()
    c, _ = _client(monkeypatch, [], secret="")
    with pytest.raises(h.NoGateway):
        c.sign_in()
    c, _ = _client(monkeypatch, [OSError("refused")])
    with pytest.raises(h.NoGateway):
        c.sign_in()
    c, _ = _client(monkeypatch, [403])
    with pytest.raises(RuntimeError, match="HTTP 403"):
        c.call("GET", "/api/chat/slots")
    c, _ = _client(monkeypatch, [OSError("gone")])
    with pytest.raises(h.NoGateway):
        c.call("GET", "/api/chat/slots")


# ---------- ledger read ----------
def test_ledger_items_read_the_owner_board(monkeypatch):
    class Board(FakeGateway):
        def call(self, method, path, body=None, timeout=60):
            if path.startswith("/api/crew-board?conductor="):
                self.calls.append((method, path, body))
                if "chat-0-" in path:
                    raise RuntimeError('GET /api/crew-board: HTTP 404 {"code": "no_ledger"}')
                return {"items": [_item("accepted", "dashboard:chat-10-1"), "junk"]}
            return super().call(method, path, body, timeout)

    gw = Board([REPLY])
    items = h.ledger_items(gw, "chat-9-1800000000")
    assert ("GET", "/api/crew-board?conductor=chat-9-1800000000", None) in gw.calls
    assert h.item_counts(items) == {"total": 1, "terminal": 1, "open": 0}
    assert h.worker_slots(items) == {"chat-10-1"}
    assert h.ledger_items(gw, "chat-0-1") == []
    assert h.transcript_path("chat-9-1800000000").parts[-2:] == (
        "sessions",
        "dashboard_chat-9-1800000000.jsonl",
    )


def test_main_prints_text_and_maps_run_errors(monkeypatch, capsys):
    monkeypatch.setattr("kiro_crew.port_resolution.resolve_client_port", lambda port=None: 1)

    def fail(*a, **k):
        raise RuntimeError("POST /api/chat/slots: HTTP 500")

    monkeypatch.setattr(h, "run", fail)
    assert h.main(_parse(["--headless", "-m", "go"])) == h.EXIT_ERROR
    assert "error: asks=0" in capsys.readouterr().err

    def odd(*a, **k):
        raise ValueError("not json")

    monkeypatch.setattr(h, "run", odd)
    assert h.main(_parse(["--headless", "-m", "go", "--json"])) == h.EXIT_ERROR
    assert json.loads(capsys.readouterr().out)["error"] == "ValueError: not json"

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(h, "run", interrupted)
    assert h.main(_parse(["--headless", "-m", "go"])) == h.EXIT_INTERRUPTED


def test_cli_dispatch_refuses_headless_flags_without_headless(monkeypatch):
    from kiro_crew import cli

    monkeypatch.setattr(sys, "argv", ["kirocrew", "chat", "-m", "x", "--until-done"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == h.EXIT_USAGE
