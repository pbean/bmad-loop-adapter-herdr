"""Integration tests for the herdr backend, gated on a live herdr install.

These mirror ``test_generic_tmux.py``'s three live tests — fake-CLI end-to-end,
crash detection, and pipe_pane log growth — but drive them through the
:class:`~bmad_loop_adapter_herdr.backend.HerdrMultiplexer` against a REAL herdr
0.7.3 server. As with the tmux ones a tiny script stands in for the CLI
binary (it writes its own SessionStart/result.json/Stop, exactly what a
hook-instrumented session produces), so spawn / env-propagation / hook-signal
waiting / window-death / kill are exercised end-to-end.

Isolation: each test runs under its own ``HERDR_SESSION=bmad-test-<uuid>`` — a
private per-session herdr server + socket (``~/.config/herdr/sessions/<name>/``)
— and forces the herdr backend by name (``BMAD_LOOP_MUX_BACKEND=herdr``); the
sidecar is redirected into ``tmp_path``. A finalizer stops+deletes that session
so no server, workspace, or poller thread outlives the test. The whole module is
skipped when herdr is not installed. It runs on win32 too: the fakes are Python
scripts landed via ``write_portable_cli`` (a ``.cmd`` shim there), exercising
the ``agent start`` launch surface live.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from bmad_loop.adapters import multiplexer
from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.generic import GenericTmuxAdapter
from bmad_loop.adapters.profile import get_profile
from bmad_loop.policy import LimitsPolicy, Policy

from _stories_recipe import write_portable_cli
from bmad_loop_adapter_herdr import backend as herdr_backend

HAVE_HERDR = shutil.which("herdr") is not None
pytestmark = pytest.mark.skipif(not HAVE_HERDR, reason="herdr not available")

# Same hook-instrumented fake as test_generic_tmux.py's FAKE_CLI (Python, so
# write_portable_cli can spawn it on either platform family): the last
# positional arg is the rendered prompt; the run dir + task id ride the pane env
# (herdr `--env`, where tmux used `-e`). Emits SessionStart + result.json + Stop,
# then idles like a live interactive session until its window is killed.
FAKE_CLI = """import json
import os
import sys
import time

prompt = sys.argv[-1]
rd = os.environ["BMAD_LOOP_RUN_DIR"]
tid = os.environ["BMAD_LOOP_TASK_ID"]
ts = time.time_ns()
os.makedirs(os.path.join(rd, "events"), exist_ok=True)
os.makedirs(os.path.join(rd, "tasks", tid), exist_ok=True)


def event(ts_ns, name):
    path = os.path.join(rd, "events", "%d-%s-%s.json" % (ts_ns, tid, name))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"ts": ts_ns, "event": name, "task_id": tid, "session_id": "fake-1"}, fh)


event(ts, "SessionStart")
with open(os.path.join(rd, "tasks", tid, "result.json"), "w", encoding="utf-8") as fh:
    json.dump({"workflow": "auto-dev", "prompt": prompt}, fh)
event(ts + 1, "Stop")
time.sleep(60)  # stay alive like an idle interactive session
"""


def _teardown_session(name: str) -> None:
    """Tear down an isolated herdr session (its server + socket + everything under
    it). Best-effort: a never-started session makes both verbs harmless no-ops."""
    for verb in ("stop", "delete"):
        subprocess.run(["herdr", "session", verb, name], capture_output=True, text=True)


@pytest.fixture
def herdr_session(tmp_path, monkeypatch):
    """Isolate the herdr backend onto a private per-test server/socket and force it
    selected by name, with a guaranteed teardown.

    ``HERDR_SESSION`` gives every ``herdr`` subprocess this backend spawns its own
    server + socket, so tests never touch the user's default session or each other
    (safe under xdist — the name is unique per test). ``BMAD_LOOP_MUX_BACKEND=herdr``
    + a cache clear on both ends makes ``get_multiplexer()`` pick herdr regardless
    of host platform (and not leak the pick). The sidecar is redirected out of
    ``~/.bmad-loop``. The finalizer stops+deletes the session even on failure."""
    name = f"bmad-test-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("HERDR_SESSION", name)
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "herdr")
    monkeypatch.setenv("BMAD_LOOP_HERDR_STATE", str(tmp_path / "herdr-state.json"))
    multiplexer.get_multiplexer.cache_clear()
    try:
        yield name
    finally:
        multiplexer.get_multiplexer.cache_clear()
        _teardown_session(name)


def _make_adapter(
    tmp_path, profile_name="claude", binary=None, extra_args=None, **policy_kw
) -> GenericTmuxAdapter:
    # Unique run dir per adapter => unique session name (== workspace label), so
    # concurrent tests on one isolated server never race a teardown vs a create.
    run_dir = tmp_path / f"run-{uuid.uuid4().hex[:8]}"
    policy = Policy(limits=LimitsPolicy(**policy_kw) if policy_kw else LimitsPolicy())
    return GenericTmuxAdapter(
        run_dir=run_dir,
        policy=policy,
        profile=get_profile(profile_name),
        binary=binary,
        extra_args=extra_args,
    )


def _write_fake_cli(tmp_path: Path, body: str = FAKE_CLI) -> Path:
    return write_portable_cli(tmp_path, "fake-cli", body)


@pytest.mark.parametrize("profile_name", ["claude", "codex", "gemini"])
def test_herdr_end_to_end_with_fake_cli(tmp_path, herdr_session, profile_name):
    """Spawn a real herdr workspace/tab/pane running a fake CLI that behaves like a
    hook-instrumented session (SessionStart + result.json + Stop) -> completed. The
    same shape as the tmux end-to-end test, proving the herdr transport carries the
    full launch/env/hook/read-back path for every profile."""
    fake = _write_fake_cli(tmp_path)
    # extra_args=() drops the bypass flags so the rendered prompt is the last argv
    # entry for every profile (claude/codex positional, gemini behind -i).
    adapter = _make_adapter(tmp_path, profile_name=profile_name, binary=str(fake), extra_args=())
    # Guard: we are genuinely exercising herdr, not a silent fall-back to tmux.
    assert isinstance(adapter.mux, herdr_backend.HerdrMultiplexer)
    spec = SessionSpec(
        task_id="t-int-1",
        role="dev",
        prompt="/bmad-dev-auto 1-1-a",
        cwd=tmp_path,
        env={
            "BMAD_LOOP_MODE": "1",
            "BMAD_LOOP_RUN_DIR": str(adapter.run_dir),
            "BMAD_LOOP_TASK_ID": "t-int-1",
        },
        timeout_s=30.0,
    )
    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json["workflow"] == "auto-dev"
    # the fake echoes back the rendered prompt it received via the pane env
    assert result.result_json["prompt"] == adapter.profile.render_prompt(spec.prompt)
    assert result.session_id == "fake-1"
    assert (adapter.tasks_dir / "t-int-1" / "prompt.txt").read_text().strip() == spec.prompt


def test_herdr_crash_detected(tmp_path, herdr_session):
    """A session that dies without writing result.json -> crashed. Pins Phase-0 O1:
    an exec'd process exit vanishes the pane (no linger), so pane-presence liveness
    reports window death authoritatively — the guarantee the SessionEnd-less codex
    path relies on."""
    fake = _write_fake_cli(tmp_path, "import sys\nsys.exit(1)\n")
    adapter = _make_adapter(
        tmp_path, profile_name="codex", binary=str(fake), stop_without_result_nudges=0
    )
    assert isinstance(adapter.mux, herdr_backend.HerdrMultiplexer)
    spec = SessionSpec(
        task_id="t-crash",
        role="dev",
        prompt="x",
        cwd=tmp_path,
        env={"BMAD_LOOP_RUN_DIR": str(adapter.run_dir), "BMAD_LOOP_TASK_ID": "t-crash"},
        timeout_s=20.0,
    )
    result = adapter.run(spec)

    assert result.status == "crashed"
    assert result.result_json is None


def test_herdr_pipe_pane_log_grows_under_real_pane(tmp_path, herdr_session, monkeypatch):
    """pipe_pane tees a live pane's output into the log by polling (herdr has no
    native pipe-pane). Under a pane emitting changing text the log must GROW and
    the latest snapshot must be discoverable in it — the two consumers a tmux tee
    drives: generic._log_activity_key (dev-stall re-arm on log growth) and probe
    (completion-marker scan). A #85-style no-op tee would leave the log flat and
    mis-stall a long silent-but-working turn. kill_session then retires the tee so
    no poller thread outlives the workspace it watched."""
    # Shrink the poll interval so the growth window is a couple of seconds, not
    # tens. _PanePoller reads this global at construction (pipe_pane time).
    monkeypatch.setattr(herdr_backend, "POLL_INTERVAL_S", 0.25)
    # Prints a fresh non-blank line ~5×/s (blank repaints aren't logged), then
    # idles alive so the pane stays readable while we watch the log.
    script = write_portable_cli(
        tmp_path,
        "grow",
        "import time\n"
        "for i in range(1, 41):\n"
        '    print("MARKER line %d" % i, flush=True)\n'
        "    time.sleep(0.2)\n"
        "time.sleep(30)\n",
    )

    mux = multiplexer.get_multiplexer()
    assert isinstance(mux, herdr_backend.HerdrMultiplexer)
    session = "bmad-loop-grow"
    mux.new_session(session, tmp_path)
    try:
        # The contract command is a POSIX shlex-joined argv string on EVERY
        # platform (a raw win32 path's backslashes would be eaten by the
        # launch's shlex re-split) — quote it exactly as core's build_command does.
        window_id = mux.new_window(session, "grow", tmp_path, {}, shlex.quote(str(script)))
        log_file = tmp_path / "grow.log"
        mux.pipe_pane(window_id, log_file)

        # pipe_pane primes the first snapshot synchronously, so the log exists at
        # once; wait for a later poll to append more (growth == the activity signal).
        first_size: int | None = None
        grew = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if log_file.exists():
                size = log_file.stat().st_size
                if first_size is None:
                    first_size = size
                elif size > first_size:
                    grew = True
                    break
            time.sleep(0.2)

        assert grew, "pipe_pane poller never grew the log under a producing pane"
        assert "MARKER" in log_file.read_text(encoding="utf-8")  # probe-marker discoverability
    finally:
        # kill_session stops the session's tees (poller registry emptied) even
        # when an assertion above fails — a leaked poller would outlive the
        # fixture's server teardown (transport failures never self-retire it)
        # and spin subprocess reads for the rest of the pytest worker's life.
        mux.kill_session(session)
    assert mux._pollers == {}


def test_herdr_parked_window_via_start_detached(herdr_session, tmp_path):
    """PR-2 surface, live: launch.start_detached parks `bmad-loop validate` in a
    bmad-loop-ctl workspace tab. Pins the whole chain: tab-label discovery
    (ctl_window through the list_windows join), the exit banner rendering while
    the window PARKS (stays alive) after the command exits, set_return_pane by
    tmux-style name target mirroring into the per-window return file, and Enter
    unparking — the trailer consumes the file, the pane closes (which is what
    ends a watching `terminal attach` client), and the ctl workspace survives
    on its root shell tab."""
    import re

    from bmad_loop.tui import launch

    run_id = "20260713-000000-p6live"
    win_id = launch.start_detached(
        tmp_path, ["validate", "--project", str(tmp_path)], run_id, "run"
    )
    assert win_id  # the native window id (the tab's root pane)
    mux = multiplexer.get_multiplexer()
    assert isinstance(mux, herdr_backend.HerdrMultiplexer)  # no silent tmux fall-back

    # discovery: the ctl window comes back by its tab label
    assert launch.ctl_window(run_id) == f"run-{run_id}"

    # validate exits quickly; the window must PARK with the EXPANDED banner
    # (the typed recipe itself contains the literal `$ec` text, so match the
    # substituted exit status, not the echo's source)
    banner_re = re.compile(r"\[bmad-loop exited \d+")
    deadline = time.monotonic() + 30
    screen, last = "", None
    while time.monotonic() < deadline:
        last = subprocess.run(
            ["herdr", "pane", "read", win_id, "--source", "recent-unwrapped"],
            capture_output=True,
            text=True,
        )
        screen = last.stdout
        if banner_re.search(screen):
            break
        time.sleep(0.5)
    if not banner_re.search(screen):
        # Distinguish "pane rendered nothing" from "pane is GONE" (a read on a
        # dead pane errors with empty stdout, indistinguishable from a blank
        # screen without rc/stderr) — the difference points at opposite bugs.
        try:
            alive: object = mux.window_alive(launch.CTL_SESSION, win_id)
        except Exception as exc:  # transport-level: report, don't mask the assert
            alive = f"unknown ({exc})"
        raise AssertionError(
            f"no parked banner in pane after validate: screen={screen[-500:]!r} "
            f"read_rc={last.returncode if last else None} "
            f"read_stderr={(last.stderr if last else '').strip()[-300:]!r} "
            f"window_alive={alive}"
        )
    assert mux.window_alive(launch.CTL_SESSION, win_id) is True  # parked, not closed

    # attach-time return recording by tmux-style name target lands in the
    # per-window return file (the sidecar key is normalized to the native id)
    launch.set_return_pane(f"={launch.CTL_SESSION}:run-{run_id}", launch.RETURN_DETACH)
    retfile = herdr_backend._return_file(win_id)
    assert retfile.read_text(encoding="utf-8").strip() == launch.RETURN_DETACH
    assert mux.show_window_option(win_id, launch.RETURN_OPTION) == launch.RETURN_DETACH

    # Enter unparks: the trailer consumes the file, sh exits, the pane closes
    subprocess.run(["herdr", "pane", "send-keys", win_id, "enter"], capture_output=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not mux.window_alive(launch.CTL_SESSION, win_id) and not retfile.exists():
            break
        time.sleep(0.5)
    assert mux.window_alive(launch.CTL_SESSION, win_id) is False
    assert not retfile.exists()  # the trailer rm -f'd it
    assert mux.has_session(launch.CTL_SESSION) is True  # root shell tab keeps it alive
