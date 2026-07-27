"""Conformance tests for the herdr terminal-multiplexer backend.

No real herdr server: every test drives :class:`HerdrMultiplexer` against a fake
that stands in for ``subprocess.run`` (the ONE spawn seam), maintaining an
in-memory server (workspaces + panes) and recording every argv so we can pin the
exact CLI each contract method emits. Mirrors ``tests/test_multiplexer.py``'s
``_RecordRun`` + seam-honesty patterns for the herdr (non-tmux-family) backend.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from bmad_loop.adapters.multiplexer import MultiplexerError

from bmad_loop_adapter_herdr import backend as herdr_backend
from bmad_loop_adapter_herdr.backend import HerdrError, HerdrMultiplexer

PROJECT_OPTION = "@bmad_project"
RETURN_OPTION = "@bmad_return_pane"


# --------------------------------------------------------------- fake transport


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


class FakeHerdr:
    """Stand-in for ``subprocess.run(["herdr", ...])``. Holds an in-memory server
    (a running flag, a protocol number, workspaces, panes), answers the CLI verbs
    this backend uses with herdr's real envelope shapes, and records every argv."""

    def __init__(self, *, running: bool = True, protocol: int = 17) -> None:
        self.running = running
        self.protocol = protocol
        self.workspaces: list[dict] = []  # {"label","workspace_id","active_tab_id"}
        self.tabs: list[dict] = []  # {"tab_id","label","number","workspace_id","focused"}
        self.panes: list[dict] = []  # {"pane_id","workspace_id","tab_id","terminal_id"}
        self.calls: list[list[str]] = []
        # Scripted `pane read` output per pane: a list of successive raw-text
        # screens; each read advances the cursor and sticks on the last entry.
        self.pane_reads: dict[str, list[str]] = {}
        self._pane_read_idx: dict[str, int] = {}
        # Seedable `pane process-info` payloads: pane_id -> process_info dict
        # (0.7.5 shape); an unseeded pane answers pane_not_found.
        self.process_infos: dict[str, dict] = {}
        # `pane rename` labels the win32 typed launch applies: pane_id -> name.
        self.pane_labels: dict[str, str] = {}
        # When True, `pane wait-output` answers rc=1 (a prompt-readiness timeout)
        # so a test can prove the win32 typed launch proceeds anyway.
        self.wait_output_fails = False
        self._ws_seq = 0
        self._tab_seq: dict[str, int] = {}
        self._pane_seq: dict[str, int] = {}
        self._term_seq = 0

    # ---- helpers a test can call to seed state

    def _new_tab(self, wid: str, label: str | None, *, focus: bool) -> dict:
        self._tab_seq[wid] = self._tab_seq.get(wid, 0) + 1
        number = self._tab_seq[wid]
        tab = {
            # real herdr gives an unlabelled tab its NUMBER as the label (a
            # workspace's root shell tab is "1" — verified 0.7.5)
            "tab_id": f"{wid}:t{number}",
            "label": label if label is not None else str(number),
            "number": number,
            "workspace_id": wid,
            "focused": False,
        }
        self.tabs.append(tab)
        if focus:
            self._focus_tab(tab)
        return tab

    def _focus_tab(self, tab: dict) -> None:
        for other in self.tabs:
            if other["workspace_id"] == tab["workspace_id"]:
                other["focused"] = other is tab
        for ws in self.workspaces:
            if ws["workspace_id"] == tab["workspace_id"]:
                ws["active_tab_id"] = tab["tab_id"]

    def _new_pane(self, wid: str, tab_id: str) -> dict:
        self._pane_seq[wid] = self._pane_seq.get(wid, 0) + 1
        self._term_seq += 1
        pane = {
            "pane_id": f"{wid}:p{self._pane_seq[wid]}",
            "workspace_id": wid,
            "tab_id": tab_id,
            "terminal_id": f"term{self._term_seq}",
        }
        self.panes.append(pane)
        return pane

    def add_workspace(self, label: str) -> str:
        """Create a workspace (with its root shell tab+pane) and return its id."""
        self._ws_seq += 1
        wid = f"w{self._ws_seq}"
        self.workspaces.append({"label": label, "workspace_id": wid, "active_tab_id": None})
        tab = self._new_tab(wid, None, focus=True)  # the root shell tab, label "1"
        self._new_pane(wid, tab["tab_id"])
        return wid

    def set_pane_reads(self, pane_id: str, screens: list[str]) -> None:
        """Script the raw-text screens successive ``pane read`` calls return."""
        self.pane_reads[pane_id] = list(screens)
        self._pane_read_idx[pane_id] = 0

    def _pane_read_out(self, pane_id: str) -> str:
        screens = self.pane_reads.get(pane_id)
        if not screens:
            return ""  # a live-but-unscripted pane reads as a blank screen
        idx = self._pane_read_idx.get(pane_id, 0)
        self._pane_read_idx[pane_id] = idx + 1
        return screens[min(idx, len(screens) - 1)]

    def set_process_info(self, pane_id: str, info: dict) -> None:
        """Seed the ``process_info`` a ``pane process-info --pane <id>`` returns;
        an unseeded pane answers ``pane_not_found``."""
        self.process_infos[pane_id] = info

    # ---- CompletedProcess builders

    @staticmethod
    def _cp(cmd, rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    def _ok(self, cmd, result: dict) -> subprocess.CompletedProcess:
        return self._cp(cmd, 0, out=json.dumps({"id": "cli:1", "result": result}))

    def _server_err(self, cmd, code: str, msg: str) -> subprocess.CompletedProcess:
        # herdr's nested error body (bare {"code",...} only for `pane read`).
        return self._cp(
            cmd, 1, err=json.dumps({"error": {"code": code, "message": msg}, "id": "x"})
        )

    def _bare_err(self, cmd, code: str, msg: str) -> subprocess.CompletedProcess:
        # `pane read` uses herdr's BARE error body (no {"error": ...} envelope).
        return self._cp(cmd, 1, err=json.dumps({"code": code, "message": msg}))

    def _down(self, cmd) -> subprocess.CompletedProcess:
        # server down / bogus socket: a non-JSON transport error.
        return self._cp(cmd, 1, err='Error: Os { code: 2, kind: NotFound, message: "nope" }')

    # ---- subprocess.run replacement

    def __call__(self, cmd, **kwargs) -> subprocess.CompletedProcess:
        assert cmd[0] == "herdr"
        argv = list(cmd[1:])
        self.calls.append(argv)
        return self._dispatch(cmd, argv)

    def _dispatch(self, cmd, argv: list[str]) -> subprocess.CompletedProcess:
        if argv == ["--version"]:
            return self._cp(cmd, 0, out="herdr 0.7.5")
        if argv[:2] == ["status", "--json"]:
            server = {"running": self.running, "protocol": self.protocol if self.running else None}
            return self._cp(cmd, 0, out=json.dumps({"server": server}))
        group = argv[0] if argv else ""
        if group == "agent":
            # herdr 0.7.5 redesigned `agent start` to only start KNOWN agent kinds
            # in an existing pane; the general argv-spawning form the win32 launch
            # used to ride is gone. The backend must emit ZERO agent verbs now — a
            # loud tripwire (clap's rc=2 "unexpected argument") so a regression
            # reviving the old path fails the launch instead of silently no-op'ing.
            return self._cp(cmd, 2, err="error: unexpected argument found")
        if not self.running and group in {"workspace", "pane", "tab"}:
            return self._down(cmd)
        verb = argv[1] if len(argv) > 1 else ""
        if group == "workspace" and verb == "list":
            return self._ok(cmd, {"workspaces": self.workspaces})
        if group == "workspace" and verb == "create":
            wid = self.add_workspace(_flag(argv, "--label"))
            ws = next(w for w in self.workspaces if w["workspace_id"] == wid)
            root = next(p for p in self.panes if p["workspace_id"] == wid)
            return self._ok(cmd, {"workspace": ws, "root_pane": root})
        if group == "workspace" and verb == "close":
            wid = argv[2]
            self.workspaces = [w for w in self.workspaces if w["workspace_id"] != wid]
            self.tabs = [t for t in self.tabs if t["workspace_id"] != wid]
            self.panes = [p for p in self.panes if p["workspace_id"] != wid]
            return self._ok(cmd, {"type": "ok"})
        if group == "tab" and verb == "create":
            wid = _flag(argv, "--workspace")
            label = _flag(argv, "--label") if "--label" in argv else None
            tab = self._new_tab(wid, label, focus="--focus" in argv)
            root = self._new_pane(wid, tab["tab_id"])
            return self._ok(cmd, {"tab": tab, "root_pane": root})
        if group == "tab" and verb == "list":
            # real herdr RAISES workspace_not_found for an absent --workspace
            # (never []); the backend must therefore always enumerate bare.
            if "--workspace" in argv:
                wid = _flag(argv, "--workspace")
                if all(w["workspace_id"] != wid for w in self.workspaces):
                    return self._server_err(
                        cmd, "workspace_not_found", f"workspace {wid} not found"
                    )
                return self._ok(cmd, {"tabs": [t for t in self.tabs if t["workspace_id"] == wid]})
            return self._ok(cmd, {"tabs": self.tabs})
        if group == "tab" and verb == "focus":
            tab = next((t for t in self.tabs if t["tab_id"] == argv[2]), None)
            if tab is None:
                return self._server_err(cmd, "tab_not_found", f"tab {argv[2]} not found")
            self._focus_tab(tab)
            return self._ok(cmd, {"tab": tab, "type": "tab_info"})
        if group == "pane" and verb == "list":
            return self._ok(cmd, {"panes": self.panes})
        if group == "pane" and verb == "get":
            pane = next((p for p in self.panes if p["pane_id"] == argv[2]), None)
            if pane is None:
                return self._server_err(cmd, "pane_not_found", f"pane {argv[2]} not found")
            return self._ok(cmd, {"pane": pane})
        if group == "pane" and verb == "process-info":
            pane_id = _flag(argv, "--pane")
            info = self.process_infos.get(pane_id)
            if info is None:
                return self._server_err(cmd, "pane_not_found", f"pane {pane_id} not found")
            return self._ok(cmd, {"process_info": info})
        if group == "pane" and verb == "close":
            pane = next((p for p in self.panes if p["pane_id"] == argv[2]), None)
            self.panes = [p for p in self.panes if p["pane_id"] != argv[2]]
            if pane is not None:  # a pane close cascades its now-empty tab closed
                tab_id = pane["tab_id"]
                if all(p["tab_id"] != tab_id for p in self.panes):
                    self.tabs = [t for t in self.tabs if t["tab_id"] != tab_id]
                    for ws in self.workspaces:
                        if ws.get("active_tab_id") == tab_id:
                            ws["active_tab_id"] = next(
                                (
                                    t["tab_id"]
                                    for t in self.tabs
                                    if t["workspace_id"] == ws["workspace_id"]
                                ),
                                None,
                            )
            return self._ok(cmd, {"type": "ok"})
        if group == "pane" and verb == "read":
            pane_id = argv[2]
            if not any(p["pane_id"] == pane_id for p in self.panes):
                return self._bare_err(cmd, "pane_not_found", f"pane {pane_id} not found")
            # pane read prints RAW TEXT, not a JSON envelope.
            return self._cp(cmd, 0, out=self._pane_read_out(pane_id))
        if group == "pane" and verb == "rename":
            # Best-effort win32 sidebar label — record it; the backend never
            # inspects the result (see _label_pane).
            self.pane_labels[argv[2]] = argv[3]
            return self._ok(cmd, {"type": "ok"})
        if group == "pane" and verb == "wait-output":
            # win32 prompt-readiness wait: rc=1 on timeout (scriptable via
            # wait_output_fails so a test can prove the launch proceeds anyway),
            # else ok when the pane exists. Faithful to herdr 0.7.5's CLI: the
            # pane_id is the FIRST positional after the verb (`pane wait-output
            # <pane_id> --regex ...`); a flag in that slot is rejected as an
            # unknown option, so a regression to pane_id-last is caught here.
            pane_id = argv[2] if len(argv) > 2 else ""
            if pane_id.startswith("--"):
                return self._server_err(cmd, "unknown_option", f"unknown option: {pane_id}")
            if self.wait_output_fails:
                return self._server_err(cmd, "wait_output_timeout", "no match before timeout")
            if not any(p["pane_id"] == pane_id for p in self.panes):
                return self._server_err(cmd, "pane_not_found", f"pane {pane_id} not found")
            return self._ok(cmd, {"type": "ok"})
        if group == "pane" and verb in {"run", "send-text", "send-keys"}:
            return self._ok(cmd, {"type": "ok"})
        return self._ok(cmd, {"type": "ok"})  # permissive fallback


def install_fake_herdr(monkeypatch, tmp_path) -> FakeHerdr:
    """Wire a FakeHerdr in as the transport: patch the spawn seam + binary probe,
    redirect the sidecar into ``tmp_path``, and clear the inside-a-pane marker.
    Shared with ``test_herdr_poller``'s ``fake`` fixture (which adds its
    poll-cadence patches on top) so the two setups can't drift."""
    f = FakeHerdr()
    monkeypatch.setattr(herdr_backend.subprocess, "run", f)
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setenv("BMAD_LOOP_HERDR_STATE", str(tmp_path / "herdr-state.json"))
    monkeypatch.delenv("HERDR_ENV", raising=False)
    return f


@pytest.fixture
def fake(monkeypatch, tmp_path):
    return install_fake_herdr(monkeypatch, tmp_path)


@pytest.fixture
def fake_posix(fake, monkeypatch):
    """The FakeHerdr transport with the launch dispatch pinned to the POSIX
    path, so the typed-exec tests keep testing typed exec when the suite runs
    on a real Windows host (the win32 twin is fake_win32, below)."""
    monkeypatch.setattr(herdr_backend, "_is_win32", lambda: False)
    return fake


def _creates(fake: FakeHerdr, group: str, verb: str) -> list[list[str]]:
    return [c for c in fake.calls if c[:2] == [group, verb]]


# ------------------------------------------------------------- availability


def test_available_and_version(fake):
    mux = HerdrMultiplexer()
    assert mux.available() is True
    assert mux.version() == "herdr 0.7.5"


def test_available_false_without_binary(monkeypatch):
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: None)
    mux = HerdrMultiplexer()
    assert mux.available() is False
    assert mux.version() is None


def test_constructor_does_no_io(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("constructor spawned a subprocess")

    monkeypatch.setattr(herdr_backend.subprocess, "run", boom)
    monkeypatch.setattr(herdr_backend.subprocess, "Popen", boom)
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: "/usr/bin/herdr")
    mux = HerdrMultiplexer()  # must not spawn anything
    assert mux.available() is True  # shutil.which only — never the server


# ------------------------------------------------------------- exact argv


def test_new_session_creates_workspace_argv(fake):
    cwd = str(Path("/work"))  # backend stringifies the Path; '\\work' on win32
    HerdrMultiplexer().new_session("bmad-loop-x", Path("/work"), 220, 50)
    creates = _creates(fake, "workspace", "create")
    assert creates == [
        ["workspace", "create", "--label", "bmad-loop-x", "--cwd", cwd, "--no-focus"]
    ]


def test_new_session_guards_duplicate_label(fake):
    mux = HerdrMultiplexer()
    mux.new_session("bmad-loop-x", Path("/work"))
    mux.new_session("bmad-loop-x", Path("/work"))  # already present -> no second create
    assert len(_creates(fake, "workspace", "create")) == 1
    assert len([w for w in fake.workspaces if w["label"] == "bmad-loop-x"]) == 1


def test_new_window_tab_create_and_exec_launch(fake_posix):
    fake = fake_posix
    cwd = str(Path("/work"))  # backend stringifies the Path; '\\work' on win32
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    pane_id = mux.new_window("bmad-loop-x", "win", Path("/work"), {"A": "1", "B": "2"}, "echo hi")
    (tab_create,) = _creates(fake, "tab", "create")
    assert tab_create == [
        "tab", "create",
        "--workspace", "w1",
        "--label", "win",
        "--cwd", cwd,
        "--env", "A=1",
        "--env", "B=2",
        "--no-focus",
    ]  # fmt: skip
    (pane_run,) = _creates(fake, "pane", "run")
    assert pane_run == ["pane", "run", pane_id, "exec echo hi"]
    assert pane_id.startswith("w1:")


def test_new_window_missing_workspace_raises(fake):
    with pytest.raises(HerdrError):
        HerdrMultiplexer().new_window("bmad-loop-absent", "win", Path("/w"), {}, "echo hi")


def test_new_window_shlex_resplit_roundtrip(fake_posix):
    fake = fake_posix
    # The contract hands new_window a POSIX shlex-joined argv (generic.build_command
    # = " ".join(shlex.quote(a) ...)). The exec launch must re-split it faithfully:
    # shlex.split(command) then shlex.join back, so a tricky arg survives intact.
    import shlex

    argv = ["claude", "-p", "hello world", "--dangerously-skip", "a&&b"]
    command = " ".join(shlex.quote(a) for a in argv)
    fake.add_workspace("bmad-loop-x")
    HerdrMultiplexer().new_window("bmad-loop-x", "win", Path("/w"), {}, command)
    (pane_run,) = _creates(fake, "pane", "run")
    launched = pane_run[3]
    assert launched == "exec " + shlex.join(argv)
    assert shlex.split(launched)[0] == "exec"
    assert shlex.split(launched)[1:] == argv


def test_send_text_literal_then_sleep_then_enter(fake, monkeypatch):
    sleeps: list[tuple[float, int]] = []
    # record the call count at sleep time to pin the ordering: paste, THEN sleep,
    # THEN submit.
    monkeypatch.setattr(herdr_backend.time, "sleep", lambda s: sleeps.append((s, len(fake.calls))))
    HerdrMultiplexer().send_text("w1:p1", "hello world")
    assert fake.calls == [
        ["pane", "send-text", "w1:p1", "hello world"],
        ["pane", "send-keys", "w1:p1", "enter"],
    ]
    assert sleeps == [(0.3, 1)]  # exactly one 0.3s sleep, after the paste (1 call so far)


# ------------------------------------------------------------- liveness honesty


def test_list_window_ids_absent_workspace_returns_empty(fake):
    # server reachable (running), label absent -> honest [] (not "couldn't ask").
    assert HerdrMultiplexer().list_window_ids("bmad-loop-x") == []


def test_list_window_ids_filters_by_workspace(fake):
    fake.add_workspace("bmad-loop-a")
    fake.add_workspace("bmad-loop-b")
    fake.add_workspace("bmad-loop-a")  # duplicate label -> first-match resolution
    mux = HerdrMultiplexer()
    ids_a = mux.list_window_ids("bmad-loop-a")
    ids_b = mux.list_window_ids("bmad-loop-b")
    assert ids_a and all(i.startswith("w1:") for i in ids_a)  # first "a" workspace wins
    assert ids_b and all(i.startswith("w2:") for i in ids_b)
    assert set(ids_a).isdisjoint(ids_b)


def test_list_window_ids_raises_when_server_unreachable(fake):
    fake.running = False
    with pytest.raises(HerdrError):
        HerdrMultiplexer().list_window_ids("bmad-loop-x")


def test_window_alive_pane_get_outcomes(fake):
    fake.add_workspace("bmad-loop-x")
    pane_id = fake.panes[-1]["pane_id"]
    mux = HerdrMultiplexer()
    assert mux.window_alive("bmad-loop-x", pane_id) is True  # present
    assert mux.window_alive("bmad-loop-x", "w9:p9") is False  # pane_not_found (answered)
    fake.running = False
    with pytest.raises(HerdrError):  # Error: Os (unreachable) -> unknowable -> raise
        mux.window_alive("bmad-loop-x", pane_id)


# ------------------------------------------------------------- contract drift
#
# This package tracks bmad-loop @main, so a seam method can be widened upstream
# without anything here going red: Python does not check the override, and a
# stale return type just degrades at runtime. bmad-loop #227 was exactly that —
# `detach_client` went `None` -> `bool`, and a `None` from this backend kept
# working while quietly meaning "nothing detached". This guard turns the next one
# into a failing test instead.


def test_implements_every_seam_method_with_the_seam_signature():
    import inspect

    from bmad_loop.adapters.multiplexer import TerminalMultiplexer

    def sig(owner, name):
        # Normalize whitespace only: both modules use `from __future__ import
        # annotations`, so the annotations compare as the source strings they are.
        return " ".join(str(inspect.signature(getattr(owner, name))).split())

    drift = {}
    for name in sorted(TerminalMultiplexer.__abstractmethods__):
        assert not getattr(
            getattr(HerdrMultiplexer, name), "__isabstractmethod__", False
        ), f"HerdrMultiplexer does not implement the abstract seam method {name!r}"
        seam, ours = sig(TerminalMultiplexer, name), sig(HerdrMultiplexer, name)
        if seam != ours:
            drift[name] = (seam, ours)
    assert not drift, f"signature drift against the bmad-loop seam: {drift}"


# ----------------------------------------------------------------- seam honesty
#
# No herdr contract method may leak a raw subprocess.TimeoutExpired / OSError:
# raisers re-raise as the seam type, sentinels degrade to their documented value.
# set_session_option is a sidecar write (no subprocess), so its transport-failure
# mode is an OSError on the file, tested separately below — not here.


@pytest.fixture(params=[subprocess.TimeoutExpired(["herdr"], 30), FileNotFoundError("herdr")])
def boom(request, monkeypatch, tmp_path):
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: "/usr/bin/herdr")

    def _boom(*_a, **_k):
        raise request.param

    monkeypatch.setattr(herdr_backend.subprocess, "run", _boom)
    monkeypatch.setenv("BMAD_LOOP_HERDR_STATE", str(tmp_path / "herdr-state.json"))
    monkeypatch.delenv("HERDR_ENV", raising=False)


def test_seam_methods_never_leak_raw_subprocess_error(boom, tmp_path):
    mux = HerdrMultiplexer()
    raisers = [
        lambda: mux.list_window_ids("s"),
        lambda: mux.window_alive("s", "w1:p1"),
        lambda: mux.has_session("s"),
        lambda: mux.new_session("s", tmp_path),
        lambda: mux.new_window("s", "n", tmp_path, {}, "cmd"),
        lambda: mux.send_text("w1:p1", "hi"),
        lambda: mux.new_parked_window("s", "n", tmp_path, ["echo", "hi"], ""),
    ]
    for call in raisers:
        with pytest.raises(MultiplexerError) as excinfo:
            call()
        assert not isinstance(excinfo.value, subprocess.SubprocessError)
        assert not isinstance(excinfo.value, OSError)

    # Sentinel returners degrade to the documented value, never raise.
    assert mux.list_windows("s", ["pane_id"]) == []
    assert mux.show_window_option("w1:p1", "opt") == ""
    assert mux.switch_client("s") is False
    assert mux.switch_client("s", last_fallback=True) is False
    assert mux.kill_window("w1:p1") is None
    assert mux.kill_session("s") is None
    assert mux.select_window("w1:p1") is None
    assert mux.set_window_option("w1:p1", "opt", "v") is None
    assert mux.unset_window_option("w1:p1", "opt") is None
    assert mux.detach_client() is False
    assert mux.pipe_pane("w1:p1", tmp_path / "log") is None
    assert mux.list_sessions() == []
    assert mux.session_options("opt") == {}
    assert mux.version() is None
    assert mux.current_pane_id() is None
    assert mux.current_window_id() is None
    assert mux.current_session() is None
    assert mux.window_pane_pids("w1:p1") == []


# ------------------------------------------------------------------- sidecar


def test_session_option_roundtrip(fake):
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    assert mux.session_options(PROJECT_OPTION) == {"bmad-loop-x": "/proj"}


def test_session_option_persists_across_instances(fake):
    HerdrMultiplexer().set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    fake.add_workspace("bmad-loop-x")
    assert HerdrMultiplexer().session_options(PROJECT_OPTION) == {"bmad-loop-x": "/proj"}


def test_session_options_prunes_dead_workspace(fake):
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    assert mux.session_options(PROJECT_OPTION) == {"bmad-loop-x": "/proj"}

    fake.workspaces.clear()  # workspace gone out-of-band
    fake.panes.clear()
    assert mux.session_options(PROJECT_OPTION) == {}
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert "bmad-loop-x" not in state["sessions"]  # sidecar entry pruned


def test_session_options_no_prune_when_unreachable(fake):
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    fake.running = False  # can't prove anything dead -> prune nothing, return {}
    assert mux.session_options(PROJECT_OPTION) == {}
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert state["sessions"]["bmad-loop-x"][PROJECT_OPTION] == "/proj"


def test_sidecar_write_is_atomic_no_temp_left(fake, tmp_path):
    HerdrMultiplexer().set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    target = tmp_path / "herdr-state.json"
    assert json.loads(target.read_text())["sessions"]["bmad-loop-x"][PROJECT_OPTION] == "/proj"
    assert list(tmp_path.glob("herdr-state.json.tmp*")) == []  # no half-written temp


def test_set_session_option_raises_on_write_failure(fake, monkeypatch):
    def boom_replace(_tmp, _target):
        raise OSError("disk full")

    monkeypatch.setattr(herdr_backend.platform_util, "atomic_replace", boom_replace)
    with pytest.raises(HerdrError):
        HerdrMultiplexer().set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")


def test_sidecar_cycles_hold_the_advisory_lock(fake, monkeypatch):
    """Every read-modify-write cycle runs UNDER the sidecar lock — while
    _save_state executes, a non-blocking acquire of the lock file must fail.
    Guards the lost-update hole: an unlocked cycle would let a concurrent
    writer's update (engine tag vs TUI prune) be silently clobbered."""
    real_save = herdr_backend._save_state
    lock_path = Path(os.environ["BMAD_LOOP_HERDR_STATE"] + ".lock")
    held: list[bool] = []

    def probing_save(state):
        try:
            with herdr_backend.platform_util.file_lock(lock_path, blocking=False):
                held.append(False)
        except OSError:
            held.append(True)
        real_save(state)

    monkeypatch.setattr(herdr_backend, "_save_state", probing_save)
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")  # raiser cycle
    mux.set_window_option("w1:p1", RETURN_OPTION, "w1:p2")  # sentinel cycle
    mux.unset_window_option("w1:p1", RETURN_OPTION)  # sentinel cycle
    fake.workspaces.clear()  # workspace gone -> next enumeration prunes
    fake.panes.clear()
    mux.session_options(PROJECT_OPTION)  # locked prune cycle
    assert held == [True, True, True, True]


def test_sidecar_lock_failure_rides_the_raiser_sentinel_split(fake, monkeypatch):
    """A lock-acquisition failure is a sidecar-write failure: the raiser wraps
    it as HerdrError, the sentinels no-op — and nobody ever writes unguarded."""

    def no_lock(_path, **_kw):
        raise OSError("lock unavailable")

    monkeypatch.setattr(herdr_backend.platform_util, "file_lock", no_lock)
    saves: list[dict] = []
    monkeypatch.setattr(herdr_backend, "_save_state", saves.append)
    mux = HerdrMultiplexer()
    with pytest.raises(HerdrError):
        mux.set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    mux.set_window_option("w1:p1", RETURN_OPTION, "w1:p2")  # sentinel: swallowed
    mux.unset_window_option("w1:p1", RETURN_OPTION)  # sentinel: swallowed
    herdr_backend._drop_state("windows", "w1:p1")  # sentinel: swallowed
    assert saves == []  # a failed lock skips the write entirely


def test_window_option_roundtrip(fake):
    mux = HerdrMultiplexer()
    assert mux.show_window_option("w1:p1", RETURN_OPTION) == ""
    mux.set_window_option("w1:p1", RETURN_OPTION, "w1:p2")
    assert mux.show_window_option("w1:p1", RETURN_OPTION) == "w1:p2"
    mux.unset_window_option("w1:p1", RETURN_OPTION)
    assert mux.show_window_option("w1:p1", RETURN_OPTION) == ""


# --------------------------------------------------------------- teardown ops


def test_kill_window_closes_pane_and_prunes_sidecar(fake):
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]
    mux = HerdrMultiplexer()
    mux.set_window_option(pane["pane_id"], "opt", "v")
    mux.kill_window(pane["pane_id"])
    assert pane not in fake.panes  # pane closed (cascades the empty tab)
    assert mux.show_window_option(pane["pane_id"], "opt") == ""  # sidecar entry gone


def test_kill_session_closes_workspace_and_prunes_sidecar(fake):
    wid = fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    mux.kill_session("bmad-loop-x")
    assert all(w["workspace_id"] != wid for w in fake.workspaces)
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert "bmad-loop-x" not in state["sessions"]


def test_kill_session_tolerates_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: None)
    monkeypatch.setenv("BMAD_LOOP_HERDR_STATE", str(tmp_path / "s.json"))
    assert HerdrMultiplexer().kill_session("bmad-loop-x") is None  # no server op, no raise


def test_list_sessions(fake):
    fake.add_workspace("bmad-loop-a")
    fake.add_workspace("bmad-loop-b")
    assert set(HerdrMultiplexer().list_sessions()) == {"bmad-loop-a", "bmad-loop-b"}


def test_list_sessions_empty_when_server_down(fake):
    fake.running = False
    assert HerdrMultiplexer().list_sessions() == []


# ---------------------------------------------------------- server + protocol


def test_ensure_server_starts_when_down(fake, monkeypatch):
    fake.running = False
    popen_calls: list[tuple] = []

    def fake_popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        fake.running = True  # the spawned server comes up
        return object()

    monkeypatch.setattr(herdr_backend.subprocess, "Popen", fake_popen)
    mux = HerdrMultiplexer()
    mux.new_session("bmad-loop-x", Path("/work"))
    assert popen_calls and popen_calls[0][0] == ["herdr", "server"]
    # detached spawn (POSIX start_new_session / win32 creationflags)
    assert "start_new_session" in popen_calls[0][1] or "creationflags" in popen_calls[0][1]
    assert any(w["label"] == "bmad-loop-x" for w in fake.workspaces)


def test_ensure_server_is_probed_once(fake):
    mux = HerdrMultiplexer()
    mux.new_session("bmad-loop-a", Path("/work"))
    mux.new_window("bmad-loop-a", "win", Path("/work"), {}, "echo hi")
    # the server was confirmed up once, not re-probed before every mutating op
    assert len([c for c in fake.calls if c[:2] == ["status", "--json"]]) == 1


def test_protocol_below_supported_raises(fake):
    fake.protocol = herdr_backend.SUPPORTED_PROTOCOL - 1
    with pytest.raises(HerdrError):
        HerdrMultiplexer().new_session("bmad-loop-x", Path("/work"))


def test_protocol_above_supported_warns_but_proceeds(fake):
    fake.protocol = herdr_backend.SUPPORTED_PROTOCOL + 1
    mux = HerdrMultiplexer()
    with pytest.warns(UserWarning):
        mux.new_session("bmad-loop-x", Path("/work"))
    assert _creates(fake, "workspace", "create")  # created despite the warning


# --------------------------------------------------------------- degradations


def test_pipe_pane_tolerates_dead_pane_and_detach_is_false(fake, tmp_path):
    # pipe_pane races a pane that already died on launch: the priming read gets
    # pane_not_found, so no tee thread is spun up (tmux swallows the same race).
    mux = HerdrMultiplexer()
    assert mux.pipe_pane("w9:p9", tmp_path / "log") is None
    assert mux._pollers == {}  # nothing left running
    # herdr has no detach verb and no client to measure, so the seam's honest
    # answer is False — never a vacuous True, which would tell an attended sweep
    # the human got their terminal back (bmad-loop #227).
    assert mux.detach_client() is False


# ------------------------------------------------- TUI-launch surface (PR 2)


def test_new_parked_window_recipe(fake_posix):
    fake = fake_posix
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    pane_id = mux.new_parked_window(
        "bmad-loop-ctl", "run-RID", Path("/work"), ["python", "-m", "x"], RETURN_OPTION
    )
    (tab_create,) = _creates(fake, "tab", "create")
    assert tab_create == [
        "tab", "create",
        "--workspace", "w1",
        "--label", "run-RID",
        "--cwd", str(Path("/work")),
        "--no-focus",
    ]  # fmt: skip
    (pane_run,) = _creates(fake, "pane", "run")
    assert pane_run[:3] == ["pane", "run", pane_id]
    typed = pane_run[3]
    # the typed line execs `sh -c '<recipe>'` (one line, POSIX+fish-safe quoting)
    assert typed.startswith("exec sh -c ")
    sh_argv = shlex.split(typed[len("exec ") :])
    assert sh_argv[:2] == ["sh", "-c"]
    src = sh_argv[2]
    assert "\n" not in typed
    # recipe shape: argv; exit capture; banner; park; return trailer
    assert src.startswith("python -m x; ec=$?; ")
    assert "[bmad-loop exited $ec — press enter]" in src
    # `read -r _`, never the bare bashism: dash (/bin/sh on Debian/Ubuntu)
    # errors on a read with no var operand and the park falls through
    assert "read -r _; " in src
    retfile = herdr_backend._return_file(pane_id)
    assert str(retfile) in src  # the trailer cats/rms the per-window return file
    assert 'herdr tab focus "$ret"' in src
    # the sidecar remembers which option this window's trailer consumes
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert state["windows"][pane_id][herdr_backend._PARKED_RETURN_KEY] == RETURN_OPTION


def test_new_parked_window_missing_session_raises(fake, tmp_path):
    with pytest.raises(HerdrError):
        HerdrMultiplexer().new_parked_window("s", "n", tmp_path, ["echo", "hi"], RETURN_OPTION)


def test_new_parked_window_rolls_back_tab_when_recipe_typing_fails(fake_posix, monkeypatch):
    fake = fake_posix
    # tmux gets create+launch atomically (one new-window call); here the tab
    # already exists when the recipe-typing `pane run` fails, so the backend
    # must close it again — not leave an untracked idle shell in the ctl
    # workspace with a stale sidecar entry.
    fake.add_workspace("bmad-loop-ctl")
    before = {p["pane_id"] for p in fake.panes}
    real_dispatch = fake._dispatch
    seen: dict[str, str] = {}

    def failing_pane_run(cmd, argv):
        if argv[:2] == ["pane", "run"]:
            seen["pane_id"] = argv[2]
            return fake._server_err(cmd, "internal", "pane run exploded")
        return real_dispatch(cmd, argv)

    monkeypatch.setattr(fake, "_dispatch", failing_pane_run)
    with pytest.raises(HerdrError):
        HerdrMultiplexer().new_parked_window(
            "bmad-loop-ctl", "run-RID", Path("/w"), ["x"], RETURN_OPTION
        )
    assert seen["pane_id"] not in before  # the launch really created a fresh pane
    assert {p["pane_id"] for p in fake.panes} == before  # ...and rolled it back
    assert _creates(fake, "pane", "close")  # via an explicit close, not luck
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert seen["pane_id"] not in state["windows"]  # sidecar entry pruned too


def test_new_parked_window_rolls_back_tab_when_sidecar_write_fails(fake_posix, monkeypatch):
    fake = fake_posix
    # The sidecar write sits between tab create and recipe typing; when it
    # fails there is no sidecar entry to catch the tab later, so the rollback
    # is the only thing standing between us and an orphan.
    fake.add_workspace("bmad-loop-ctl")
    before = {p["pane_id"] for p in fake.panes}

    def no_lock(_path, **_kw):
        raise OSError("lock unavailable")

    monkeypatch.setattr(herdr_backend.platform_util, "file_lock", no_lock)
    with pytest.raises(HerdrError):
        HerdrMultiplexer().new_parked_window(
            "bmad-loop-ctl", "run-RID", Path("/w"), ["x"], RETURN_OPTION
        )
    assert {p["pane_id"] for p in fake.panes} == before  # created tab rolled back
    assert _creates(fake, "pane", "close")
    assert not _creates(fake, "pane", "run")  # recipe never typed into a doomed tab


def test_parked_return_option_round_trip(fake):
    # cmd_attach / the TUI write the return option by tmux-style name target;
    # return_attached_client (inside the window) reads it back by native id —
    # the sidecar key is normalized so both agree. The return FILE holds the
    # trailer-actionable TAB id of the origin pane, resolved at write time.
    fake.add_workspace("bmad-loop-ctl")
    fake.add_workspace("userws")
    origin = fake.panes[-1]  # the TUI's own pane, in the user's workspace
    mux = HerdrMultiplexer()
    pane_id = mux.new_parked_window("bmad-loop-ctl", "sweep-RID", Path("/w"), ["x"], RETURN_OPTION)
    retfile = herdr_backend._return_file(pane_id)

    mux.set_window_option("=bmad-loop-ctl:sweep-RID", RETURN_OPTION, origin["pane_id"])
    assert mux.show_window_option(pane_id, RETURN_OPTION) == origin["pane_id"]
    assert retfile.read_text(encoding="utf-8").strip() == origin["tab_id"]

    # RETURN_DETACH is mirrored verbatim (the trailer treats it as "do nothing":
    # ending the source closes the pane, which ends a `terminal attach` client)
    mux.set_window_option(pane_id, RETURN_OPTION, "detach")
    assert retfile.read_text(encoding="utf-8").strip() == "detach"

    # unsetting clears the option AND the file (so the post-exit trailer cannot
    # fire a second time after a mid-process return)
    mux.unset_window_option(pane_id, RETURN_OPTION)
    assert mux.show_window_option(pane_id, RETURN_OPTION) == ""
    assert not retfile.exists()


def test_parked_return_unresolvable_origin_clears_file(fake):
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    pane_id = mux.new_parked_window("bmad-loop-ctl", "run-RID", Path("/w"), ["x"], RETURN_OPTION)
    mux.set_window_option(pane_id, RETURN_OPTION, "detach")
    retfile = herdr_backend._return_file(pane_id)
    assert retfile.exists()
    # a gone origin pane must not leave a stale focus target behind
    mux.set_window_option(pane_id, RETURN_OPTION, "w9:p9")
    assert not retfile.exists()
    assert mux.show_window_option(pane_id, RETURN_OPTION) == "w9:p9"  # option itself kept


def test_kill_window_by_name_target_cleans_return_file(fake):
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    pane_id = mux.new_parked_window("bmad-loop-ctl", "run-RID", Path("/w"), ["x"], RETURN_OPTION)
    mux.set_window_option(pane_id, RETURN_OPTION, "detach")
    retfile = herdr_backend._return_file(pane_id)
    assert retfile.exists()
    mux.kill_window("=bmad-loop-ctl:run-RID")  # kill_ctl_window passes this form
    assert all(p["pane_id"] != pane_id for p in fake.panes)
    assert not retfile.exists()
    assert mux.show_window_option(pane_id, RETURN_OPTION) == ""


def test_kill_session_cleans_window_sidecar_and_return_files(fake):
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    pane_id = mux.new_parked_window("bmad-loop-ctl", "run-RID", Path("/w"), ["x"], RETURN_OPTION)
    mux.set_window_option(pane_id, RETURN_OPTION, "detach")
    retfile = herdr_backend._return_file(pane_id)
    mux.kill_session("bmad-loop-ctl")
    assert not retfile.exists()
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert pane_id not in state["windows"]


def test_select_window_by_name_focuses_tab(fake):
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    pane_id = mux.new_window("bmad-loop-ctl", "run-RID", Path("/w"), {}, "echo hi")
    tab_id = next(p["tab_id"] for p in fake.panes if p["pane_id"] == pane_id)
    mux.select_window("=bmad-loop-ctl:run-RID")
    assert ["tab", "focus", tab_id] in fake.calls
    ws = next(w for w in fake.workspaces if w["label"] == "bmad-loop-ctl")
    assert ws["active_tab_id"] == tab_id
    # unresolvable targets stay quiet no-ops (sentinel)
    assert mux.select_window("=bmad-loop-ctl:nope") is None
    assert mux.select_window("=absent:x") is None


def test_attach_target_argv_native_and_name_targets(fake):
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    pane_id = mux.new_window("bmad-loop-ctl", "run-RID", Path("/w"), {}, "echo hi")
    pane = next(p for p in fake.panes if p["pane_id"] == pane_id)
    expected = ["herdr", "terminal", "attach", pane["terminal_id"]]
    assert mux.attach_target_argv(pane_id) == expected  # native pane id
    assert mux.attach_target_argv("=bmad-loop-ctl:run-RID") == expected  # by window name


def test_attach_target_argv_session_level(fake):
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    a = mux.new_window("bmad-loop-ctl", "run-a", Path("/w"), {}, "sleep 1")
    b = mux.new_window("bmad-loop-ctl", "run-b", Path("/w"), {}, "sleep 1")
    term = {p["pane_id"]: p["terminal_id"] for p in fake.panes}
    # task tabs are created --no-focus, so the root shell tab is still active:
    # a session-level attach prefers the NEWEST task tab over the root shell
    assert mux.attach_target_argv("=bmad-loop-ctl")[-1] == term[b]
    # after an explicit select (attach_plan runs select_ctl_window first) the
    # active tab wins
    mux.select_window("=bmad-loop-ctl:run-a")
    assert mux.attach_target_argv("=bmad-loop-ctl")[-1] == term[a]
    assert a != b


def test_attach_target_argv_session_level_single_tab_is_root(fake):
    fake.add_workspace("bmad-loop-x")
    root = fake.panes[-1]
    assert HerdrMultiplexer().attach_target_argv("=bmad-loop-x")[-1] == root["terminal_id"]


def test_attach_target_argv_inside_herdr_focuses(fake, monkeypatch):
    # inside a herdr pane, nesting a full attach is the wrong move (as with
    # attach-inside-tmux): return the fire-and-forget focus argv instead
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    pane_id = mux.new_window("bmad-loop-ctl", "run-RID", Path("/w"), {}, "echo hi")
    tab_id = next(p["tab_id"] for p in fake.panes if p["pane_id"] == pane_id)
    monkeypatch.setenv("HERDR_ENV", "1")
    assert mux.attach_target_argv("=bmad-loop-ctl:run-RID") == ["herdr", "tab", "focus", tab_id]


def test_attach_target_argv_missing_pane_raises(fake):
    with pytest.raises(HerdrError):
        HerdrMultiplexer().attach_target_argv("w9:p9")


def test_attach_target_argv_missing_session_raises_with_guidance(fake):
    with pytest.raises(HerdrError) as excinfo:
        HerdrMultiplexer().attach_target_argv("=absent")
    assert "attach" in str(excinfo.value)


def test_switch_client_focuses_origin_tab(fake):
    fake.add_workspace("bmad-loop-ctl")
    fake.add_workspace("userws")
    origin = fake.panes[-1]
    mux = HerdrMultiplexer()
    assert mux.switch_client(origin["pane_id"]) is True
    assert ["tab", "focus", origin["tab_id"]] in fake.calls
    # a gone target is honestly False; herdr has no "last client" to fall back to
    assert mux.switch_client("w9:p9") is False
    assert mux.switch_client("w9:p9", last_fallback=True) is False


def test_list_windows_tmux_field_mapping(fake):
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    pane_id = mux.new_window("bmad-loop-ctl", "run-RID", Path("/w"), {}, "echo hi")
    mux.set_window_option(pane_id, PROJECT_OPTION, "/proj")
    rows = mux.list_windows("bmad-loop-ctl", ["window_id", "window_name", PROJECT_OPTION])
    by_id = {row[0]: row for row in rows}
    assert by_id[pane_id] == (pane_id, "run-RID", "/proj")
    root = fake.panes[0]["pane_id"]
    assert by_id[root] == (root, "1", "")  # the root shell tab: numeric label, untagged
    # ctl_window()'s discovery contract: every window comes back with its name
    assert {row[1] for row in rows} == {"1", "run-RID"}


def test_in_ctl_session_seam_honest_under_herdr(fake, monkeypatch):
    # launch.in_ctl_session no longer sniffs TMUX: with herdr selected and the
    # herdr pane env present it resolves through the seam.
    from bmad_loop.adapters.multiplexer import get_multiplexer
    from bmad_loop.tui import launch

    wid = fake.add_workspace(launch.CTL_SESSION)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "herdr")
    get_multiplexer.cache_clear()
    try:
        monkeypatch.setenv("HERDR_ENV", "1")
        monkeypatch.setenv("HERDR_WORKSPACE_ID", wid)
        monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
        assert launch.in_ctl_session() is True
        monkeypatch.setenv("HERDR_WORKSPACE_ID", "w9")  # some other workspace
        assert launch.in_ctl_session() is False
        monkeypatch.delenv("HERDR_ENV", raising=False)  # not inside herdr at all
        assert launch.in_ctl_session() is False
    finally:
        get_multiplexer.cache_clear()


def test_current_accessors_resolve_from_env(fake, monkeypatch):
    wid = fake.add_workspace("bmad-loop-x")
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", wid)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    mux = HerdrMultiplexer()
    assert mux.current_session() == "bmad-loop-x"
    assert mux.current_pane_id() == "w1:p1"
    assert mux.current_window_id() == "w1:p1"


def test_current_accessors_none_outside_herdr(fake, monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    mux = HerdrMultiplexer()
    assert mux.current_session() is None
    assert mux.current_pane_id() is None
    assert mux.current_window_id() is None


# ------------------------------------------------- bmad-loop 0.9.0 kill/return seam


def test_window_pane_pids(fake):
    fake.add_workspace("bmad-loop-x")
    pane_id = fake.panes[-1]["pane_id"]
    mux = HerdrMultiplexer()

    # Seeded process-info: shell_pid leads, then each foreground pid; a pid that
    # appears in BOTH (the shell is its own group leader) is deduped, not doubled.
    fake.set_process_info(
        pane_id,
        {
            "pane_id": pane_id,
            "shell_pid": 4242,
            "foreground_process_group_id": 4242,
            "foreground_processes": [
                {"pid": 4242, "name": "pwsh", "argv": ["pwsh"]},
                {"pid": 4288, "name": "python", "argv": ["python", "-m", "x"]},
            ],
            "tty": "/dev/pts/3",
        },
    )
    assert mux.window_pane_pids(pane_id) == [4242, 4288]

    # shell_pid null (herdr reports no shell process) -> only the foreground pids.
    fake.set_process_info(
        pane_id,
        {"pane_id": pane_id, "shell_pid": None, "foreground_processes": [{"pid": 77}]},
    )
    assert mux.window_pane_pids(pane_id) == [77]

    # A pane herdr doesn't know answers pane_not_found -> [] ("unknown", not "none").
    assert mux.window_pane_pids("w9:p9") == []

    # Server down: the pane group errors transport-level -> [].
    fake.running = False
    assert mux.window_pane_pids(pane_id) == []


def test_current_return_target_default_is_pane_id(monkeypatch):
    # bmad-loop 0.9.0 seam: herdr DELIBERATELY inherits the default
    # current_return_target (native pane id). herdr's one server makes a pane id
    # unique server-wide, so it resolves from any other session's context and
    # needs no qualification — psmux (one server PER session) overrides this;
    # herdr must not. The absent __dict__ entry pins the no-override decision.
    assert "current_return_target" not in HerdrMultiplexer.__dict__
    mux = HerdrMultiplexer()
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    assert mux.current_return_target() == "w1:p1"
    monkeypatch.delenv("HERDR_ENV", raising=False)  # not inside herdr -> None
    assert mux.current_return_target() is None


# ------------------------------------------------ win32 launch surface (PR 3)


def test_pwsh_quote_literals():
    # Single-quoted literals: the ONLY escape is doubling the quote; everything
    # PowerShell would otherwise interpret ($, `, ", ;, &, |) stays literal.
    assert herdr_backend._pwsh_quote("abc") == "'abc'"
    assert herdr_backend._pwsh_quote("it's") == "'it''s'"
    assert herdr_backend._pwsh_quote('$env:X "q" ;&|') == "'$env:X \"q\" ;&|'"
    assert herdr_backend._pwsh_quote("") == "''"
    assert herdr_backend._pwsh_quote("C:\\Users\\x y") == "'C:\\Users\\x y'"


def test_pwsh_binary_selection(monkeypatch):
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: "/bin/pwsh")
    assert herdr_backend._pwsh_binary() == "pwsh"
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: None)
    assert herdr_backend._pwsh_binary() == "powershell"


def test_typed_launch_pwsh_shape():
    # `& <pwsh-quoted argv>; exit $LASTEXITCODE`: the call operator runs argv and
    # `exit` ends the shell with the child's status (process-exit == pane-close,
    # the POSIX `exec` mirror). _pwsh_quote is the ONLY quoting layer.
    assert herdr_backend._typed_launch_pwsh(["echo", "hi"]) == "& 'echo' 'hi'; exit $LASTEXITCODE"
    line = herdr_backend._typed_launch_pwsh(["c md", "a&&b", "it's"])
    assert line == "& 'c md' 'a&&b' 'it''s'; exit $LASTEXITCODE"  # spaces/&& stay literal
    assert "\n" not in line


def test_parked_source_pwsh_shape(fake):
    pane_id = "w1:p1"
    src = herdr_backend._parked_source_pwsh(["python", "-m", "x", "it's"], pane_id)
    assert "\n" not in src
    # recipe shape: encoding prelude; argv; exit capture; banner; park; trailer
    assert src.startswith("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; ")
    assert "& 'python' '-m' 'x' 'it''s'; $ec=$LASTEXITCODE" in src
    assert "if ($null -eq $ec) { $ec=127 }" in src  # POSIX command-not-found status
    assert '"[bmad-loop exited $ec — press enter]"' in src  # byte-identical banner
    assert "Read-Host | Out-Null" in src
    # the pane id exists at compose time (the recipe is TYPED, not spawned), so
    # the return-file path is embedded as a pwsh literal — no runtime
    # HERDR_PANE_ID / Join-Path derivation (the POSIX recipe's posture, mirrored)
    rf = herdr_backend._pwsh_quote(str(herdr_backend._return_file(pane_id)))
    assert f"Get-Content -LiteralPath {rf} " in src
    assert f"Remove-Item -LiteralPath {rf} " in src
    assert "$env:HERDR_PANE_ID" not in src
    assert "Join-Path" not in src
    assert "-ne 'detach'" in src
    assert "herdr tab focus $ret *> $null" in src


@pytest.fixture
def fake_win32(fake, monkeypatch):
    """The FakeHerdr transport with the launch dispatch forced onto the win32
    path. Patches the backend's _is_win32 predicate, NOT sys.platform: a global
    platform lie would drag core's platform_util into the OTHER platform's lock
    branch (msvcrt vs fcntl), and the sidecar assertions want the host's REAL
    lock. The live sys.platform read itself is pinned by
    test_is_win32_reads_sys_platform_live."""
    monkeypatch.setattr(herdr_backend, "_is_win32", lambda: True)
    return fake


def test_new_window_win32_typed_pwsh_launch(fake_win32):
    fake = fake_win32
    cwd = str(Path("/work"))
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    pane_id = mux.new_window("bmad-loop-x", "win", Path("/work"), {"A": "1", "B": "2"}, "echo hi")
    # tab create carries the label (window-name resolution) AND the env now — the
    # tab's default PowerShell pane is the launch target, no doomed bootstrap
    (tab_create,) = _creates(fake, "tab", "create")
    assert tab_create == [
        "tab", "create",
        "--workspace", "w1",
        "--label", "win",
        "--cwd", cwd,
        "--env", "A=1",
        "--env", "B=2",
        "--no-focus",
    ]  # fmt: skip
    # best-effort prompt-readiness wait, then the typed pwsh launch, then a label.
    # pane_id is the FIRST positional after the verb, per herdr 0.7.5's CLI
    # (`pane wait-output <pane_id> --regex ...`) — pane_id-last makes herdr reject
    # the regex as an unknown option, silently disabling the readiness wait.
    (wait,) = _creates(fake, "pane", "wait-output")
    assert wait == [
        "pane", "wait-output",
        pane_id,
        "--regex", "PS .*>",
        "--timeout", "10000",
    ]  # fmt: skip
    (pane_run,) = _creates(fake, "pane", "run")
    assert pane_run == ["pane", "run", pane_id, "& 'echo' 'hi'; exit $LASTEXITCODE"]
    assert fake.pane_labels[pane_id] == "win"  # `pane rename` applied the sidebar label
    # single-pane tab, and ZERO agent verbs
    assert not _creates(fake, "agent", "start")
    tab_id = next(p["tab_id"] for p in fake.panes if p["pane_id"] == pane_id)
    assert [p["pane_id"] for p in fake.panes if p["tab_id"] == tab_id] == [pane_id]


def test_new_window_win32_typed_launch_pwsh_quotes_argv(fake_win32):
    # The contract command string is POSIX-shlex-joined on every platform;
    # shlex.split is its lossless inverse, and the typed launch pwsh-quotes each
    # element (single-quoted literals — the only quoting layer). Spaces and `&&`
    # stay literal inside the quotes.
    fake = fake_win32
    argv = ["claude", "-p", "hello world", "--flag", "a&&b", "C:\\Users\\x y"]
    command = " ".join(shlex.quote(a) for a in argv)
    fake.add_workspace("bmad-loop-x")
    pane_id = HerdrMultiplexer().new_window("bmad-loop-x", "win", Path("/w"), {}, command)
    (pane_run,) = _creates(fake, "pane", "run")
    expected = "& " + " ".join(herdr_backend._pwsh_quote(a) for a in argv) + "; exit $LASTEXITCODE"
    assert pane_run == ["pane", "run", pane_id, expected]
    assert "'hello world'" in pane_run[3]  # space survives, literal
    assert "'a&&b'" in pane_run[3]  # `&&` survives, literal
    assert "'C:\\Users\\x y'" in pane_run[3]


def test_new_window_win32_window_name_target_resolves_to_root_pane(fake_win32):
    # The typed launch keeps the tab single-pane (the root pane IS the window),
    # so "=session:window" targets resolve to the window id new_window handed back.
    fake = fake_win32
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    pane_id = mux.new_window("bmad-loop-x", "win", Path("/w"), {}, "echo hi")
    assert mux._parse_target("=bmad-loop-x:win", strict=True) == pane_id


def test_win32_prompt_wait_failure_still_launches(fake_win32):
    # A `pane wait-output` timeout (rc=1) is swallowed — the typed launch proceeds
    # regardless (POSIX relies on the PTY queueing input typed before the shell
    # reads it; win32 matches that posture, with the bounded wait a mere cushion).
    fake = fake_win32
    fake.wait_output_fails = True
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    pane_id = mux.new_window("bmad-loop-x", "win", Path("/w"), {}, "echo hi")
    assert _creates(fake, "pane", "wait-output")  # the wait WAS attempted...
    (pane_run,) = _creates(fake, "pane", "run")  # ...and the launch still happened
    assert pane_run == ["pane", "run", pane_id, "& 'echo' 'hi'; exit $LASTEXITCODE"]


def test_new_parked_window_win32_recipe(fake_win32):
    fake = fake_win32
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    pane_id = mux.new_parked_window(
        "bmad-loop-ctl", "run-RID", Path("/work"), ["python", "-m", "x"], RETURN_OPTION
    )
    (tab_create,) = _creates(fake, "tab", "create")
    assert tab_create == [
        "tab", "create",
        "--workspace", "w1",
        "--label", "run-RID",
        "--cwd", str(Path("/work")),
        "--no-focus",
    ]  # fmt: skip
    # the recipe is TYPED into the tab's default PowerShell pane as a nested
    # `pwsh -NoProfile -Command <recipe>`, wrapped by _typed_launch_pwsh — one
    # `pane run`, zero agent verbs
    (pane_run,) = _creates(fake, "pane", "run")
    assert pane_run[:3] == ["pane", "run", pane_id]
    typed = pane_run[3]
    assert "\n" not in typed
    binary = herdr_backend._pwsh_binary()
    recipe = herdr_backend._parked_source_pwsh(["python", "-m", "x"], pane_id)
    expected = herdr_backend._typed_launch_pwsh([binary, "-NoProfile", "-Command", recipe])
    assert typed == expected
    # nested-pwsh shape + the recipe embedded once as a single pwsh literal
    assert typed.startswith(f"& {herdr_backend._pwsh_quote(binary)} '-NoProfile' '-Command' ")
    assert typed.endswith("; exit $LASTEXITCODE")
    assert herdr_backend._pwsh_quote(recipe) in typed
    assert not _creates(fake, "agent", "start")
    # single-pane tab; the label is applied; and the sidecar recorded the
    # trailer's option under the window id BEFORE the recipe was typed (the
    # sidecar-before-typing ordering the rollback tests below pin)
    tab_id = next(p["tab_id"] for p in fake.panes if p["pane_id"] == pane_id)
    assert [p["pane_id"] for p in fake.panes if p["tab_id"] == tab_id] == [pane_id]
    assert fake.pane_labels[pane_id] == "run-RID"
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert state["windows"][pane_id][herdr_backend._PARKED_RETURN_KEY] == RETURN_OPTION


def test_new_parked_window_win32_rolls_back_when_pane_run_fails(fake_win32, monkeypatch):
    # win32's typed recipe rides `pane run` now (not agent start); a failing pane
    # run must close the created tab and prune its sidecar entry — the exact
    # mirror of the POSIX recipe-typing rollback.
    fake = fake_win32
    fake.add_workspace("bmad-loop-ctl")
    before = {p["pane_id"] for p in fake.panes}
    real_dispatch = fake._dispatch
    seen: dict[str, str] = {}

    def failing_pane_run(cmd, argv):
        if argv[:2] == ["pane", "run"]:
            seen["pane_id"] = argv[2]
            return fake._server_err(cmd, "internal", "pane run exploded")
        return real_dispatch(cmd, argv)

    monkeypatch.setattr(fake, "_dispatch", failing_pane_run)
    with pytest.raises(HerdrError):
        HerdrMultiplexer().new_parked_window(
            "bmad-loop-ctl", "run-RID", Path("/w"), ["x"], RETURN_OPTION
        )
    assert seen["pane_id"] not in before  # the launch really created a fresh pane
    assert {p["pane_id"] for p in fake.panes} == before  # ...and rolled it back
    assert _creates(fake, "pane", "close")  # via an explicit close, not luck
    # the sidecar write PRECEDED the typed recipe, so the rollback prunes it too
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert seen["pane_id"] not in state["windows"]


def test_new_parked_window_win32_rolls_back_when_sidecar_write_fails(fake_win32, monkeypatch):
    # The sidecar write sits BEFORE the typed recipe now (the pane id exists at
    # compose time), so its failure rolls the created tab back with nothing ever
    # typed — the POSIX posture, no longer a win32 special case.
    fake = fake_win32
    fake.add_workspace("bmad-loop-ctl")
    before = {p["pane_id"] for p in fake.panes}

    def no_lock(_path, **_kw):
        raise OSError("lock unavailable")

    monkeypatch.setattr(herdr_backend.platform_util, "file_lock", no_lock)
    with pytest.raises(HerdrError):
        HerdrMultiplexer().new_parked_window(
            "bmad-loop-ctl", "run-RID", Path("/w"), ["x"], RETURN_OPTION
        )
    assert {p["pane_id"] for p in fake.panes} == before  # created tab rolled back
    assert _creates(fake, "pane", "close")
    assert not _creates(fake, "pane", "run")  # recipe never typed into a doomed tab


def test_win32_parked_return_round_trip(fake_win32):
    # set_window_option by tmux-style name target on a win32 parked window
    # mirrors into _return_file(pane_id) — the same path the recipe embeds at
    # compose time.
    fake = fake_win32
    fake.add_workspace("bmad-loop-ctl")
    mux = HerdrMultiplexer()
    pane_id = mux.new_parked_window("bmad-loop-ctl", "run-RID", Path("/w"), ["x"], RETURN_OPTION)
    mux.set_window_option("=bmad-loop-ctl:run-RID", RETURN_OPTION, "detach")
    retfile = herdr_backend._return_file(pane_id)
    assert retfile.read_text(encoding="utf-8").strip() == "detach"
    mux.kill_window(pane_id)
    assert not retfile.exists()


def test_win32_launch_never_uses_agent_verbs(fake_win32):
    # The twin of test_posix_launch_never_uses_agent_start: herdr 0.7.5 has no
    # argv-spawning agent verb, so both win32 launch surfaces stay on the typed
    # pwsh `pane run` path and emit ZERO agent calls (the fake tripwires any
    # `agent` verb to rc=2 for good measure).
    fake = fake_win32
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.new_window("bmad-loop-x", "w1n", Path("/w"), {}, "echo hi")
    mux.new_parked_window("bmad-loop-x", "park", Path("/w"), ["x"], RETURN_OPTION)
    assert not [c for c in fake.calls if c and c[0] == "agent"]
    assert len(_creates(fake, "pane", "run")) == 2


def test_win32_seam_methods_never_leak_raw_subprocess_error(boom, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    mux = HerdrMultiplexer()
    for call in (
        lambda: mux.new_window("s", "n", tmp_path, {}, "cmd"),
        lambda: mux.new_parked_window("s", "n", tmp_path, ["echo", "hi"], ""),
    ):
        with pytest.raises(MultiplexerError) as excinfo:
            call()
        assert not isinstance(excinfo.value, subprocess.SubprocessError)
        assert not isinstance(excinfo.value, OSError)


def test_is_win32_reads_sys_platform_live(monkeypatch):
    # Not an import-time constant: the branch must follow a monkeypatched
    # sys.platform in either direction, whatever the real host is.
    monkeypatch.setattr(sys, "platform", "win32")
    assert herdr_backend._is_win32() is True
    monkeypatch.setattr(sys, "platform", "linux")
    assert herdr_backend._is_win32() is False


def test_run_decodes_utf8_on_win32(fake, monkeypatch):
    # _run resolves the decoding per call: locale default on POSIX (unchanged),
    # utf-8 + errors=replace on win32 (herdr emits UTF-8; a legacy code page
    # would mangle the banner's em dash, and a bad byte must not kill the tee).
    # Both legs forced explicitly so the test is host-independent.
    recorded: list[dict] = []
    real_fake = fake

    def recording_run(cmd, **kwargs):
        recorded.append(kwargs)
        return real_fake(cmd, **kwargs)

    monkeypatch.setattr(herdr_backend.subprocess, "run", recording_run)
    mux = HerdrMultiplexer()
    monkeypatch.setattr(herdr_backend, "_is_win32", lambda: False)
    mux.list_sessions()
    assert recorded[-1]["encoding"] is None
    assert recorded[-1]["errors"] is None
    monkeypatch.setattr(herdr_backend, "_is_win32", lambda: True)
    mux.list_sessions()
    assert recorded[-1]["encoding"] == "utf-8"
    assert recorded[-1]["errors"] == "replace"


def test_posix_launch_never_uses_agent_start(fake_posix):
    fake = fake_posix
    # The do-not-change-POSIX constraint, pinned: both launch surfaces on a
    # POSIX platform stay on the typed-exec path.
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.new_window("bmad-loop-x", "w1n", Path("/w"), {}, "echo hi")
    mux.new_parked_window("bmad-loop-x", "park", Path("/w"), ["x"], RETURN_OPTION)
    assert not _creates(fake, "agent", "start")
    assert len(_creates(fake, "pane", "run")) == 2
