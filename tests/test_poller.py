"""Tests for the herdr ``pipe_pane`` polling tee (:class:`_PanePoller`).

herdr has no ``pipe-pane``/tee, so :meth:`HerdrMultiplexer.pipe_pane` emulates it
with a daemon that polls ``pane read`` and appends a fresh snapshot to the log
whenever the pane content changes (content-hash gated — the CLI ``revision`` is
unusable). Two consumers depend on that log: ``generic._log_activity_key``
re-arms the dev-stall grace on log *growth*, and ``probe`` finds completion
markers in the log text. These tests reuse ``test_backend``'s in-memory
``FakeHerdr`` transport (now answering ``pane read``) and drive the poller both
directly (deterministic) and through real threads (kill / self-retire).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from test_backend import install_fake_herdr

from bmad_loop_adapter_herdr import backend as herdr_backend
from bmad_loop_adapter_herdr.backend import HerdrMultiplexer


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = install_fake_herdr(monkeypatch, tmp_path)
    # Tight cadence so thread-level tests don't sleep a real second per tick, and
    # a short not-found streak so self-retire happens fast. Read from the module
    # globals by _PanePoller.__init__, so these apply to pipe_pane-started tees.
    monkeypatch.setattr(herdr_backend, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(herdr_backend, "POLL_NOT_FOUND_LIMIT", 2)
    return f


# ------------------------------------------------------------------ helpers


def _key(path: Path) -> tuple[int, int]:
    """The exact activity signature generic._log_activity_key reads."""
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _stop_all(mux: HerdrMultiplexer) -> None:
    """Retire every tee and join it — keep the suite free of stray threads."""
    with mux._pollers_lock:
        pollers = list(mux._pollers.values())
        mux._pollers.clear()
    for poller in pollers:
        poller.stop()
        poller.join(timeout=2.0)


# --------------------------------------------------------- direct (no thread)


def test_poller_appends_on_change_skips_unchanged(fake, tmp_path):
    # The core contract: the log's (mtime_ns, size) key advances across a CHANGED
    # read and stays put across an identical one — that key is what re-arms the
    # dev-stall grace, so a static screen must not keep it alive.
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]["pane_id"]
    log = tmp_path / "logs" / "t.log"
    fake.set_pane_reads(pane, ["screen A\n", "screen A\n", "screen B\n"])

    poller = herdr_backend._PanePoller(
        herdr_backend._HerdrClient(), pane, log, interval_s=1, not_found_limit=2
    )
    assert poller.prime() is True  # read #1 "screen A" -> appended
    key1 = _key(log)

    poller._record(poller._read_snapshot())  # read #2 identical -> no growth
    assert _key(log) == key1

    poller._record(poller._read_snapshot())  # read #3 changed -> key advances
    assert _key(log) != key1
    assert log.stat().st_size > key1[1]
    text = log.read_text(encoding="utf-8")
    assert "screen A" in text and "screen B" in text


def test_poller_records_marker_for_discovery(fake, tmp_path):
    # probe/marker scanning reads the tee'd log; the latest snapshot (markers and
    # all) must land there verbatim.
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]["pane_id"]
    log = tmp_path / "logs" / "t.log"
    fake.set_pane_reads(pane, ["booting...\n", "Auto Run Result: done\nStatus: completed\n"])

    poller = herdr_backend._PanePoller(
        herdr_backend._HerdrClient(), pane, log, interval_s=1, not_found_limit=2
    )
    assert poller.prime() is True
    poller._record(poller._read_snapshot())
    assert "Auto Run Result: done" in log.read_text(encoding="utf-8")


def test_poller_blank_screen_makes_no_log(fake, tmp_path):
    # A live-but-blank pane isn't activity: prime succeeds (the pane answered) but
    # writes nothing, so _log_activity_key stays None and can't spuriously re-arm.
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]["pane_id"]
    log = tmp_path / "logs" / "t.log"
    fake.set_pane_reads(pane, ["", "   \n"])

    poller = herdr_backend._PanePoller(
        herdr_backend._HerdrClient(), pane, log, interval_s=1, not_found_limit=2
    )
    assert poller.prime() is True
    poller._record(poller._read_snapshot())
    assert not log.exists()


def test_prime_false_when_pane_already_gone(fake, tmp_path):
    # The crash-on-launch race: pane read answers pane_not_found -> no tee.
    poller = herdr_backend._PanePoller(
        herdr_backend._HerdrClient(), "w9:p9", tmp_path / "t.log", interval_s=1, not_found_limit=2
    )
    assert poller.prime() is False
    assert not (tmp_path / "t.log").exists()


def test_prime_false_when_server_unreachable(fake, tmp_path):
    # A transport hiccup at attach time is swallowed (no tee), mirroring tmux
    # pipe-pane's TmuxError tolerance — the run's death is caught elsewhere.
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]["pane_id"]
    fake.running = False  # `pane read` now returns a non-JSON `Error: Os`
    poller = herdr_backend._PanePoller(
        herdr_backend._HerdrClient(), pane, tmp_path / "t.log", interval_s=1, not_found_limit=2
    )
    assert poller.prime() is False


# ------------------------------------------------------------- threaded tees


def test_pipe_pane_tees_pane_growth(fake, tmp_path):
    # End-to-end: pipe_pane starts a tee that streams a growing pane into the log.
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]["pane_id"]
    fake.set_pane_reads(pane, ["line 1\n", "line 1\nline 2\n", "line 1\nline 2\nline 3\n"])
    log = tmp_path / "run" / "t.log"
    mux = HerdrMultiplexer()
    try:
        mux.pipe_pane(pane, log)
        assert mux._pollers[pane].is_alive()
        assert _wait_until(lambda: "line 3" in _read(log))
    finally:
        _stop_all(mux)
    text = log.read_text(encoding="utf-8")
    assert "line 1" in text and "line 2" in text and "line 3" in text


def test_pipe_pane_replaces_existing_tee(fake, tmp_path):
    # A re-armed window calls pipe_pane again; only one tee should survive.
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]["pane_id"]
    fake.set_pane_reads(pane, ["hi\n"])
    mux = HerdrMultiplexer()
    try:
        mux.pipe_pane(pane, tmp_path / "a.log")
        first = mux._pollers[pane]
        mux.pipe_pane(pane, tmp_path / "b.log")
        second = mux._pollers[pane]
        assert first is not second
        assert first.join(timeout=2.0) is None and not first.is_alive()  # old tee retired
        assert second.is_alive()
    finally:
        _stop_all(mux)


def test_kill_window_stops_tee(fake, tmp_path):
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]["pane_id"]
    fake.set_pane_reads(pane, ["hello\n"])
    mux = HerdrMultiplexer()
    mux.pipe_pane(pane, tmp_path / "t.log")
    poller = mux._pollers[pane]
    assert poller.is_alive()

    mux.kill_window(pane)
    poller.join(timeout=2.0)
    assert not poller.is_alive()
    assert pane not in mux._pollers


def test_tee_self_retires_when_pane_vanishes(fake, tmp_path):
    # No kill_* call: the pane just disappears (its process exited). The tee sees
    # POLL_NOT_FOUND_LIMIT consecutive pane_not_founds and exits on its own.
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]["pane_id"]
    fake.set_pane_reads(pane, ["alive\n"])
    mux = HerdrMultiplexer()
    try:
        mux.pipe_pane(pane, tmp_path / "t.log")
        poller = mux._pollers[pane]
        fake.panes = [p for p in fake.panes if p["pane_id"] != pane]  # process exited
        poller.join(timeout=3.0)
        assert not poller.is_alive()
    finally:
        _stop_all(mux)


def test_transport_hiccup_does_not_retire_tee(fake, tmp_path):
    # A couldn't-ask read (server briefly unreachable) is neither growth nor
    # death: the tee keeps running and resumes appending once reads recover.
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]["pane_id"]
    fake.set_pane_reads(pane, ["before\n"])
    mux = HerdrMultiplexer()
    try:
        mux.pipe_pane(pane, tmp_path / "t.log")
        poller = mux._pollers[pane]
        fake.running = False  # reads become non-JSON transport errors
        time.sleep(0.1)  # several ticks of hiccups (>> POLL_NOT_FOUND_LIMIT)
        assert poller.is_alive()  # NOT retired — a hiccup isn't a not-found
        fake.running = True
        fake.set_pane_reads(pane, ["after\n"])
        assert _wait_until(lambda: "after" in _read(tmp_path / "t.log"))
    finally:
        _stop_all(mux)


def test_kill_session_stops_all_session_tees(fake, tmp_path):
    # No thread leak across kill_session: every tee for the session's windows is
    # retired (matched by the pane-id's workspace prefix) and the registry clears.
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    pane_a = mux.new_window("bmad-loop-x", "w-a", tmp_path, {}, "echo a")
    pane_b = mux.new_window("bmad-loop-x", "w-b", tmp_path, {}, "echo b")
    fake.set_pane_reads(pane_a, ["a\n"])
    fake.set_pane_reads(pane_b, ["b\n"])
    mux.pipe_pane(pane_a, tmp_path / "a.log")
    mux.pipe_pane(pane_b, tmp_path / "b.log")
    pollers = [mux._pollers[pane_a], mux._pollers[pane_b]]
    assert all(p.is_alive() for p in pollers)

    mux.kill_session("bmad-loop-x")
    for poller in pollers:
        poller.join(timeout=2.0)
    assert not any(p.is_alive() for p in pollers)
    assert mux._pollers == {}


def test_no_herdr_poll_threads_leak(fake, tmp_path):
    # A guard on the whole module: after a tee's lifecycle nothing named
    # herdr-poll-* is left alive.
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]["pane_id"]
    fake.set_pane_reads(pane, ["x\n"])
    mux = HerdrMultiplexer()
    mux.pipe_pane(pane, tmp_path / "t.log")
    mux.kill_window(pane)
    _wait_until(lambda: not _live_poll_threads(pane))
    assert not _live_poll_threads(pane)


def _live_poll_threads(pane_id: str) -> list[threading.Thread]:
    name = f"herdr-poll-{pane_id}"
    return [t for t in threading.enumerate() if t.name == name and t.is_alive()]
