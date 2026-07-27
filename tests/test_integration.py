"""Integration tests for the herdr backend, gated on a live herdr install.

These mirror ``test_generic_tmux.py``'s three live tests — fake-CLI end-to-end,
crash detection, and pipe_pane log growth — but drive them through the
:class:`~bmad_loop_adapter_herdr.backend.HerdrMultiplexer` against a REAL herdr
0.7.5 server. As with the tmux ones a tiny script stands in for the CLI
binary (it writes its own SessionStart/result.json/Stop, exactly what a
hook-instrumented session produces), so spawn / env-propagation / hook-signal
waiting / window-death / kill are exercised end-to-end.

Isolation: each test runs on a PRIVATE herdr server — a throwaway socket plus
config/state root (``HERDR_SOCKET_PATH`` + ``XDG_CONFIG_HOME``/``XDG_STATE_HOME``;
see :func:`_isolate_herdr`), so a test never touches the user's real server or its
session state — and forces the herdr backend by name (``BMAD_LOOP_MUX_BACKEND=herdr``);
the sidecar is redirected into ``tmp_path``. A finalizer stops that server and removes
its root, so no server, workspace, or poller thread outlives the test. The whole module
is skipped when herdr is not installed. It runs on win32 too: the fakes are Python
scripts landed via ``write_portable_cli`` (a ``.cmd`` shim there), exercising the
``pane run`` typed-launch surface live.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
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


def _isolate_herdr(monkeypatch, tmp_path) -> Path:
    """Redirect every ``herdr`` this test spawns onto a PRIVATE 0.7.5 server, fully
    isolated from the user's real server and persisted session state.

    herdr 0.7.5 dropped the ``HERDR_SESSION`` env var that 0.7.3 used for a private
    per-session socket (only the ``--session`` *flag* still routes, and this backend
    spawns fixed ``herdr <verb>`` argv — it never adds a flag). Isolation is now
    three env vars, inherited by every spawned ``herdr`` and by the real
    ``bmad-loop`` child (``_run`` copies ``os.environ``):

    - ``HERDR_SOCKET_PATH`` -> a fresh socket, so ``ensure_server`` brings up a NEW
      server here instead of attaching the user's running one. Kept under a short
      ``tempfile`` dir so the path stays within ``sun_path``'s ~108-byte cap.
    - ``XDG_CONFIG_HOME`` / ``XDG_STATE_HOME`` -> a private config/state root, so the
      server restores from an EMPTY session snapshot (not the user's
      ``~/.config/herdr/session.json``) and persists its own back there — the user's
      real workspaces are never read or overwritten.

    Returns the private root; the caller's finalizer (:func:`_teardown_server`)
    stops the server and removes it. ``BMAD_LOOP_HERDR_STATE`` still redirects
    bmad-loop's own sidecar out of ``~/.bmad-loop``."""
    base = Path(tempfile.mkdtemp(prefix="hdr-"))
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(base / "h.sock"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(base / "state"))
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "herdr")
    monkeypatch.setenv("BMAD_LOOP_HERDR_STATE", str(tmp_path / "herdr-state.json"))
    return base


def _teardown_server(base: Path) -> None:
    """Stop this test's private herdr server and remove its root. Best-effort: a
    never-started server makes ``server stop`` a harmless no-op. The isolation env
    is passed explicitly (not left to monkeypatch teardown ordering) so ``server
    stop`` always routes to the private socket, never the user's default server."""
    env = {
        **os.environ,
        "HERDR_SOCKET_PATH": str(base / "h.sock"),
        "XDG_CONFIG_HOME": str(base / "cfg"),
        "XDG_STATE_HOME": str(base / "state"),
    }
    subprocess.run(["herdr", "server", "stop"], capture_output=True, text=True, env=env)
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def herdr_session(tmp_path, monkeypatch):
    """Isolate the herdr backend onto a private per-test 0.7.5 server + config/state
    root and force it selected by name, with a guaranteed teardown. See
    :func:`_isolate_herdr` for the mechanism (``HERDR_SESSION`` is gone in 0.7.5).
    ``get_multiplexer`` is cache-cleared on both ends so the pick is herdr
    regardless of host platform (and does not leak). Yields the private root."""
    base = _isolate_herdr(monkeypatch, tmp_path)
    multiplexer.get_multiplexer.cache_clear()
    try:
        yield base
    finally:
        multiplexer.get_multiplexer.cache_clear()
        _teardown_server(base)


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

        # pipe_pane's priming read only SEEDS the delta baseline (the pre-attach
        # screen is never logged), so the log appears with the first line the pane
        # renders after that; wait for a later poll to append more on top of it —
        # growth is the activity signal.
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


def test_herdr_tee_streams_output_once_under_real_pane(tmp_path, herdr_session, monkeypatch):
    """The tee is a stream, not a pile of frames: under a real pane each rendered
    line lands in the log EXACTLY once, and the log stays the size of the output
    rather than of the frames carrying it.

    That is what core's two post-mortem readers assume. Re-logging every frame
    would leave #194's 64 KiB tail reaching back seconds instead of minutes, so a
    transport error would age out unclassified, and would inflate a file whose
    SIZE is read as proof the CLI rendered something (#261, floor 256 bytes).

    What the log may still carry from before the session's own output is the
    shell's echo of the launch line — herdr launches by TYPING into the tab's
    default shell, and that echo lands within milliseconds of the priming read,
    on either side of it. Bounded by the argv's length and asserted as such here;
    the pre-attach SCREEN, which is unbounded, is what priming keeps out."""
    monkeypatch.setattr(herdr_backend, "POLL_INTERVAL_S", 0.25)
    # A deliberately SLOW producer: several ticks per line, so consecutive frames
    # always overlap and the delta alignment is exercised at its normal cadence
    # rather than at a scroll-past-the-window edge.
    script = write_portable_cli(
        tmp_path,
        "slowtalk",
        "import time\n"
        "for i in range(1, 6):\n"
        '    print("TEELINE %d" % i, flush=True)\n'
        "    time.sleep(1)\n"
        "time.sleep(30)\n",
    )

    mux = multiplexer.get_multiplexer()
    assert isinstance(mux, herdr_backend.HerdrMultiplexer)
    session = "bmad-loop-tee"
    mux.new_session(session, tmp_path)
    try:
        command = shlex.quote(str(script))
        window_id = mux.new_window(session, "tee", tmp_path, {}, command)
        log_file = tmp_path / "tee.log"
        mux.pipe_pane(window_id, log_file)

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if "TEELINE 5" in (log_file.read_text(encoding="utf-8") if log_file.exists() else ""):
                break
            time.sleep(0.2)
        text = log_file.read_text(encoding="utf-8")

        for i in range(1, 6):
            assert text.count(f"TEELINE {i}") == 1, f"line {i} not streamed exactly once:\n{text}"
        # Five rendered lines, plus at most the echoed launch line and a trailing
        # prompt. A frame-appending tee would put ~6 lines in per tick and land an
        # order of magnitude above this; the bound is what pins "stream, not pile".
        lines = [line for line in text.splitlines() if line.strip()]
        assert len(lines) <= 7, f"log is carrying frames, not output:\n{text}"
    finally:
        mux.kill_session(session)


def test_herdr_env_fault_classified_from_the_tee(tmp_path, herdr_session):
    """Cross-seam, live: a CLI that prints a transport error and then idles out its
    session clock is classified as an environment fault (bmad-loop #194) off the
    herdr tee — so the story PAUSES for a human instead of burning a dev attempt.

    Drives the real production path end to end: the pattern is the one the stock
    `claude` profile ships, the log is the poller's, and the classification is
    `run()`'s own post-mortem hook. The fake waits before printing so the line is
    rendered AFTER the tee attaches — the same ordering a real API outage has, and
    the one the seeded baseline makes load-bearing."""
    fake = _write_fake_cli(
        tmp_path,
        "import time\n"
        "time.sleep(2)\n"  # let the tee attach first: a pre-attach line is baseline
        'print("API Error: Connection refused (ECONNREFUSED)", flush=True)\n'
        "time.sleep(60)\n",  # idle out the session clock, exactly as the outage does
    )
    adapter = _make_adapter(tmp_path, profile_name="claude", binary=str(fake), extra_args=())
    assert isinstance(adapter.mux, herdr_backend.HerdrMultiplexer)
    assert adapter._env_fault_patterns, "the claude profile no longer seeds env_fault_patterns"
    spec = SessionSpec(
        task_id="t-envfault",
        role="dev",
        prompt="x",
        cwd=tmp_path,
        env={"BMAD_LOOP_RUN_DIR": str(adapter.run_dir), "BMAD_LOOP_TASK_ID": "t-envfault"},
        timeout_s=12.0,
    )
    result = adapter.run(spec)

    assert result.status == "timeout"
    assert result.result_json is None
    assert result.env_fault is True
    assert "ECONNREFUSED" in (result.env_fault_evidence or "")


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
