"""Herdr backend for bmad-loop's terminal-multiplexer seam.

Herdr (https://herdr.dev) is a cross-platform, agent-aware terminal workspace
manager: a headless background server plus a CLI, talking over a Unix domain
socket on POSIX and a **named pipe on Windows**. Implementing the
:class:`~bmad_loop.adapters.multiplexer.TerminalMultiplexer` contract on top of
it gives bmad-loop a native-Windows-capable transport *and* herdr's agent-status
sidebar for watching runs — with no core edits (a new backend is a module that
self-registers at import time; see bmad-loop's ``docs/porting-to-a-new-os.md``,
https://github.com/bmad-code-org/bmad-loop/blob/main/docs/porting-to-a-new-os.md).

Unlike the tmux family, this backend does **not** subclass the tmux base: herdr's
object model and CLI are a different binary family, so it implements the contract
fresh. The mapping (characterized against herdr 0.7.3 / protocol 16 — see the
plan's Phase-0 Findings; the win32 ``agent start`` launch shape characterized on
POSIX 0.7.3, Phase-A of issue #1, with the real-Windows-host pass tracked there):

- bmad-loop session  -> herdr **workspace** (label == the session name)
- bmad-loop window   -> herdr **tab** (one pane); the native window id we hand
  back is that pane's id (``w1:p1``-shaped on both platform families)
- the launched command runs so that process-exit == pane-close == tab-close ==
  tmux-identical window death: on POSIX via a typed ``exec <argv>`` into the
  tab's root shell (``pane run`` = type + Enter atomically); on win32 — where
  there is no ``exec`` and the default shell's dialect is unknowable — via
  ``agent start --tab``, which spawns argv DIRECTLY as a new pane's process in
  the freshly created tab (``agent start`` can only ever split an existing tab,
  never create one), after which the tab's bootstrap shell pane is closed so
  the tab is single-pane again. That close is STRICT: a lingering shell would
  be the tab's FIRST pane (shadowing ``_parse_target``'s root-pane resolution)
  and a phantom immortal window in ``list_window_ids`` — which is also why the
  win32 launch gets a rollback (both panes go) where POSIX needs none (its
  failure leaves one benign idle tab). Agent display names are uniquified with
  the tab id (``name@tab_id``): herdr agent names are server-global-unique
  while their pane lives, and two concurrent runs both have a probe window;
  nothing targets BY agent name, so the suffix is cosmetic.

All herdr subprocess I/O is funnelled through :class:`_HerdrClient` (the ``_run``
/ ``_herdr`` / ``_herdr_json`` primitives plus the server lifecycle), so a future
socket transport can replace it without touching :class:`HerdrMultiplexer`.

**Degradation ledger (PR 1 = run path; PR 2 added the TUI-launch surface; PR 3
the native-Windows launch):**

- ``pipe_pane`` has no herdr ``pipe-pane``/tee to hand off to, so it runs a
  per-window :class:`_PanePoller` daemon that snapshots ``pane read`` into the
  log whenever the pane content changes (content-hash-gated — the CLI
  ``revision`` is unusable, it stays 0). This drives the two log consumers a
  tmux tee would: ``generic._log_activity_key`` re-arms the dev-stall grace on
  log growth, and ``probe`` finds completion markers in the log.
- ``new_parked_window`` types a POSIX ``exec sh -c '<argv>; ec=$?; echo
  <banner>; read -r; <trailer>'`` recipe into a fresh tab, tmux-identical from
  the operator's seat. The tmux trailer reads the return option live via
  ``show-options``; herdr window options live in OUR sidecar, which a one-line
  ``sh`` trailer can't query — so the option methods mirror the parked window's
  return option into a per-window **return file** the trailer ``cat``\\ s
  (resolved to a *tab id* at write time, keeping the trailer a dumb
  ``herdr tab focus``). On win32 the recipe is the pwsh dialect
  (:func:`_parked_source_pwsh`, PowerShell-5.1-compatible; ``pwsh`` preferred,
  ``powershell`` fallback) riding ``agent start`` as ONE argv element — no
  typed-through-shell quoting layer exists. Its trailer derives the return-file
  path at RUNTIME from the injected ``HERDR_PANE_ID`` (the pane id doesn't
  exist at compose time — the window IS the launch's product), which also
  forces the one ordering deviation: the ``_PARKED_RETURN_KEY`` sidecar write
  lands right AFTER ``agent start`` instead of before the recipe (still before
  the method returns; residual race bounded — return-pane writes fire at
  attach time, launch-scale later).
- ``attach_target_argv`` accepts both target families (native pane id and the
  seam-canonical ``=session[:window]`` tokens core formats via
  ``TerminalMultiplexer.target`` — see ``_parse_target``): outside herdr it
  returns ``["herdr", "terminal",
  "attach", <terminal_id>]`` (which blocks, and exits when the pane closes);
  inside a herdr pane it returns the fire-and-forget ``["herdr", "tab",
  "focus", <tab_id>]`` — the switch-client move, mirroring tmux's in-``TMUX``
  branch. Raises ``HerdrError`` with operator guidance when unresolvable.
- ``switch_client`` is a ``tab focus`` on the target's tab (focusing a tab in
  another workspace flips workspace focus too — verified 0.7.3). herdr has no
  "last client" concept, so ``last_fallback`` has nothing to fall back to and
  a failed switch is honestly ``False``.
- ``detach_client`` is a no-op — herdr detach is a keybinding, with no CLI
  verb. Consequence: the *post-exit* detach return is full-fidelity anyway
  (ending the parked source closes the pane, which ends a blocking ``terminal
  attach`` client), but the *mid-process* ``RETURN_DETACH`` hand-back
  (``launch.return_attached_client`` while a plain-terminal client watches)
  degrades to "stay attached until the parked window closes".
- Session/window **options** have no native herdr equivalent, so they live in a
  cross-process **sidecar** JSON (``~/.bmad-loop/herdr-state.json``, override
  ``BMAD_LOOP_HERDR_STATE``), written via :func:`platform_util.atomic_replace`
  with every read-modify-write cycle serialized by an OS advisory lock on a
  sibling ``.lock`` file (:func:`platform_util.file_lock`) — the write
  serialization the tmux server gave ``set-option`` for free; entries for a
  workspace that is gone are pruned on the next enumeration. Window-option keys
  are **normalized to the native pane id** (``_parse_target``) so a write by
  ``=session:name`` target and a read by native id agree.
- ``new_session`` geometry (cols/lines) is **advisory** — herdr exposes no
  absolute headless resize; a detached pane takes an attaching client's size.
- Protocol policy: fail below :data:`SUPPORTED_PROTOCOL`, warn once above.

See :mod:`bmad_loop.adapters.multiplexer` for the contract and the
raisers-vs-sentinels split.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

from bmad_loop import platform_util
from bmad_loop.adapters.multiplexer import (
    MultiplexerError,
    TerminalMultiplexer,
    parse_target,
    register_multiplexer,
)

HERDR_TIMEOUT_S = 30
# The herdr server wire protocol this backend was written against (0.7.3). Read
# back from `herdr status --json` -> .server.protocol on the first server op.
SUPPORTED_PROTOCOL = 16
# How long _start_server waits for a freshly spawned `herdr server` to report
# itself running before giving up. Module-level so tests can shrink it.
SERVER_START_TIMEOUT_S = 5.0
_SERVER_POLL_S = 0.1

# pipe_pane poller cadence: how often a _PanePoller re-reads its pane, and how
# many CONSECUTIVE server-answered pane-not-founds retire it (the pane vanished
# on process exit — see Phase-0 O1). Module-level so tests can shrink them.
POLL_INTERVAL_S = 1.0
POLL_NOT_FOUND_LIMIT = 3

# Env var herdr injects into every pane it spawns; its presence is how the
# current_* accessors know this process is running inside a herdr pane.
_HERDR_ENV_MARKER = "HERDR_ENV"

# Per-window option value (vs a pane id) telling the parked trailer to detach
# the client rather than switch it — the same protocol constant the tmux family
# uses (tmux_base.PARKED_RETURN_DETACH); duplicated so this backend stays free
# of tmux imports.
PARKED_RETURN_DETACH = "detach"

# Private sidecar window-entry key recording which user option is a parked
# window's return option (new_parked_window writes it; set/unset_window_option
# consult it to mirror that option into the window's return file). "~"-prefixed
# so it can never collide with a tmux-style "@" user option.
_PARKED_RETURN_KEY = "~parked_return_opt"


def _is_win32() -> bool:
    """Live ``sys.platform`` read, never an import-time constant: the forced-win32
    unit tests (and the registration tests before them) monkeypatch
    ``sys.platform`` on this shared module, and the launch branch must follow."""
    return sys.platform == "win32"


class HerdrError(MultiplexerError):
    """A herdr transport op failed — the seam type, so call sites catch it via
    :class:`~bmad_loop.adapters.multiplexer.MultiplexerError` without importing
    this backend."""


# --------------------------------------------------------------- sidecar state
#
# herdr has no per-session/per-window user options (tmux's `set-option -t`), so
# the @bmad_project prune tag and the @bmad_return_pane launch marker are kept in
# a small JSON file shared across processes. Keyed by the durable identities
# bmad-loop already uses: session name (== workspace label) and native window id
# (== pane id). Reads tolerate a missing/corrupt file; writes go through
# atomic_replace so a concurrent reader never sees a torn file, and every
# read-modify-write cycle holds an exclusive OS advisory lock on a sibling
# `.lock` file so concurrent writers (engine tagging a new session vs the
# TUI/CLI's prune rewrite) never lose each other's updates — the serialization
# the tmux server gave `set-option` for free. Plain reads stay lock-free.
#
# Residual (deliberate): session_options takes its workspace-liveness snapshot
# OUTSIDE the lock — never hold it across a subprocess call with a 30 s timeout
# — so a session created+tagged between that enumeration and the prune can still
# lose its fresh tag. Consequence is bounded: runs.prunable_sessions falls back
# to run-dir ownership + engine liveness for untagged sessions.


def _state_path() -> Path:
    override = os.environ.get("BMAD_LOOP_HERDR_STATE")
    if override:
        return Path(override)
    return Path.home() / ".bmad-loop" / "herdr-state.json"


def _state_lock():
    """The advisory lock guarding sidecar read-modify-write cycles. Blocking:
    holders only do file I/O, so waits are micro-scale (bounded ~10 s on win32
    by msvcrt's built-in retry). Acquisition failure is an ``OSError``, riding
    each caller's existing raiser/sentinel split."""
    path = _state_path()
    return platform_util.file_lock(path.with_name(path.name + ".lock"))


def _load_state() -> dict:
    """The sidecar as a ``{"sessions": {...}, "windows": {...}}`` dict. A missing
    or unreadable/corrupt file reads as empty — never raises."""
    try:
        raw = _state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    sessions = data.get("sessions")
    windows = data.get("windows")
    return {
        "sessions": sessions if isinstance(sessions, dict) else {},
        "windows": windows if isinstance(windows, dict) else {},
    }


def _save_state(state: dict) -> None:
    """Persist the sidecar atomically. Propagates ``OSError`` — a *raiser* caller
    (set_session_option) wraps it as :class:`HerdrError`; *sentinel* callers
    swallow it."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        platform_util.atomic_replace(tmp, path)
    except OSError:
        # Don't leave a half-written temp behind on a failed swap.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ------------------------------------------------------------- return files
#
# A parked window's trailing shell must read the CURRENT value of its return
# option at park-exit time. tmux trailers run `show-options` against the live
# server; herdr window options live in the sidecar JSON above, which a one-line
# sh trailer cannot sensibly parse — so the option methods mirror the parked
# return option into a tiny per-window file the trailer `cat`s. The path is
# deterministic from the sidecar location, so the window creator (one process),
# the attach-time writer (another), and the in-window trailer (a third) all
# agree with no coordination.


def _return_file(pane_id: str) -> Path:
    # ':' is not a legal filename character on Windows, hence the '-' mapping.
    state = _state_path()
    return state.with_name(f"herdr-return-{pane_id.replace(':', '-')}")


def _write_return_file(pane_id: str, content: str) -> None:
    """Best-effort: the option write already succeeded, and the mirror only
    serves the trailer — a failure here must not fail the (sentinel) caller."""
    path = _return_file(pane_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
    except OSError:
        pass


def _remove_return_file(pane_id: str) -> None:
    try:
        _return_file(pane_id).unlink()
    except OSError:
        pass


def _parked_source(argv: list[str], pane_id: str) -> str:
    """The POSIX sh source a parked window runs (win32 dialect:
    :func:`_parked_source_pwsh`). Typed via ``pane run``, so it
    must be a single line; it passes through the pane's default shell before
    ``exec sh -c`` takes over, so it sticks to quoting every POSIX family
    member AND fish accept (single-quoted payloads with the shlex ``'"'"'``
    concatenation dance).

    After the park, the trailer hands an attached client back to its origin:
    the return file holds a tab id to focus (one ``tab focus`` also flips
    workspace focus — verified 0.7.3) or :data:`PARKED_RETURN_DETACH`, where
    doing NOTHING is correct — ending the source closes the pane, and a
    ``herdr terminal attach`` client exits when its pane closes (verified
    0.7.3)."""
    ret = shlex.quote(str(_return_file(pane_id)))
    return (
        f"{shlex.join(argv)}; ec=$?; "
        f'echo "[bmad-loop exited $ec — press enter]"; '
        "read -r; "
        f"ret=$(cat {ret} 2>/dev/null); rm -f {ret}; "
        f'if [ -n "$ret" ] && [ "$ret" != {PARKED_RETURN_DETACH} ]; then '
        f'herdr tab focus "$ret" >/dev/null 2>&1 || true; fi'
    )


def _pwsh_quote(token: str) -> str:
    """PowerShell single-quoted literal — the pwsh dialect of ``shlex.quote``.
    Inside ``'...'`` the ONLY escape is doubling the quote itself: no
    ``$``-expansion, no backtick processing, identical in Windows PowerShell 5.1
    and pwsh 7."""
    return "'" + token.replace("'", "''") + "'"


def _pwsh_binary() -> str:
    """``pwsh`` (PowerShell 7) when installed, else ``powershell`` (Windows
    PowerShell 5.1, present on every supported Windows). The parked recipe sticks
    to the 5.1-compatible subset, so the fallback is a name change, not a
    degradation."""
    return "pwsh" if shutil.which("pwsh") else "powershell"


def _parked_source_pwsh(argv: list[str]) -> str:
    """The PowerShell source a win32 parked window runs — the pwsh dialect of
    :func:`_parked_source`, statement for statement: ``&`` call operator ↔ the
    joined argv, ``$LASTEXITCODE`` ↔ ``$?`` (null-guarded to 127, the POSIX
    command-not-found status, because only native commands set it), ``Write-Host``
    ↔ ``echo`` (banner byte-identical — the ``[Console]::OutputEncoding`` prelude
    keeps its em dash intact on legacy code pages), ``Read-Host`` ↔ ``read -r``,
    ``Get-Content``/``Remove-Item -ErrorAction SilentlyContinue`` ↔
    ``cat 2>/dev/null``/``rm -f``, ``*> $null`` ↔ ``>/dev/null 2>&1 || true``.

    Passed to ``agent start`` as ONE argv element (never typed through a pane's
    default shell), so no outer quoting layer exists. Unlike the POSIX recipe the
    pane id does not exist at compose time — the window IS the launch's product —
    so the trailer derives its return-file path AT RUNTIME from ``HERDR_PANE_ID``
    (herdr injects it into every pane it spawns — Phase-A E6); only the sidecar
    DIRECTORY is embedded, captured here in the launcher so the pane needs no env
    of ours, and the ``':'→'-'`` mapping mirrors :func:`_return_file` exactly."""
    state_dir = _pwsh_quote(str(_state_path().parent))
    run = "& " + " ".join(_pwsh_quote(a) for a in argv)
    return (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"{run}; $ec=$LASTEXITCODE; if ($null -eq $ec) {{ $ec=127 }}; "
        'Write-Host "[bmad-loop exited $ec — press enter]"; '
        "Read-Host | Out-Null; "
        f"$rf=Join-Path {state_dir} "
        "('herdr-return-' + $env:HERDR_PANE_ID.Replace(':','-')); "
        "$ret=Get-Content -LiteralPath $rf -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "Remove-Item -LiteralPath $rf -ErrorAction SilentlyContinue; "
        f"if ($ret -and $ret -ne '{PARKED_RETURN_DETACH}') {{ herdr tab focus $ret *> $null }}"
    )


# ----------------------------------------------------------------- transport
#
# The ONE place herdr is spawned. Everything protocol-specific about the CLI wire
# format lives above this line as argv the multiplexer builds; the client only
# spawns, decodes, and enforces the protocol/server lifecycle. A socket transport
# would reimplement these primitives and leave HerdrMultiplexer untouched.


class _HerdrClient:
    """Isolates all herdr subprocess I/O behind three spawn primitives plus the
    server-lifecycle guard, so a socket transport can swap in later."""

    #: Output decoding for captured herdr text — an explicit override hook;
    #: resolved per call in _run: this attribute when set, else utf-8 on win32
    #: (herdr emits UTF-8 everywhere, and a Windows console's legacy code page
    #: would mangle the parked banner's em dash in `pane read` snapshots), else
    #: the locale default. Mirrors BaseTmuxBackend._ENCODING.
    _ENCODING: str | None = None

    def __init__(self) -> None:
        # Set once the server is confirmed up AND its protocol checked, so the
        # per-op ensure_server() is a cheap no-op for the rest of the run.
        self._ensured = False
        self._proto_warned = False

    def _run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """The ONE place herdr is spawned. ``argv`` are the args after ``herdr``.

        With ``check=True`` a non-zero exit raises :class:`HerdrError`; with
        ``check=False`` the completed process is returned as-is so callers can
        apply their own tolerant / server-answered-vs-transport handling. A
        timeout / missing binary always propagates raw (``TimeoutExpired`` /
        ``OSError``) — the seam-honesty guarantee is enforced one level up, in the
        helpers and contract methods, exactly as the tmux base does."""
        encoding = self._ENCODING or ("utf-8" if _is_win32() else None)
        proc = subprocess.run(
            ["herdr", *argv],
            capture_output=True,
            text=True,
            encoding=encoding,
            # A mojibake'd screen snapshot must degrade, not raise, inside the
            # poller's tee thread — replacement chars are activity, an
            # UnicodeDecodeError would kill the tee.
            errors="replace" if encoding else None,
            env=env,
            timeout=HERDR_TIMEOUT_S,
        )
        if check and proc.returncode != 0:
            raise HerdrError(f"herdr {' '.join(argv[:2])} failed: {proc.stderr.strip()}")
        return proc

    def _herdr(self, *args: str) -> str:
        """Strict spawn: non-zero already raises inside :meth:`_run`; a timeout /
        missing binary is trapped here and re-raised as the seam type. Returns the
        stripped stdout."""
        try:
            return self._run(list(args), check=True).stdout.strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr {args[0] if args else ''} failed: {exc}") from exc

    def _herdr_json(self, *args: str) -> dict:
        """Strict spawn of a structured herdr command, returning its ``result``
        object. herdr prints a single-line envelope ``{"id":..,"result":{..}}`` by
        default (no ``--json`` flag — passing one errors); a non-JSON body is a
        transport/version fault -> :class:`HerdrError`."""
        out = self._herdr(*args)
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError as exc:
            raise HerdrError(f"herdr {args[0] if args else ''} returned non-JSON: {out!r}") from exc
        result = envelope.get("result") if isinstance(envelope, dict) else None
        return result if isinstance(result, dict) else {}

    # ---- server lifecycle

    def ensure_server(self) -> None:
        """Idempotently guarantee a running, protocol-compatible server. Called
        from the session-creation ops (never from diagnostics like ``list_sessions``
        or ``detect_multiplexers``, which must not spawn a server). No CLI verb
        autostarts the server, so this is mandatory before the first mutating op.

        Warm path (server already up) is a SINGLE ``status`` read — the ``_ensured``
        flag then makes every later mutating op skip the probe entirely."""
        if self._ensured:
            return
        server = self._server()  # one status read: gives both running and protocol
        if not server.get("running"):
            self._start_server()  # polls status until running
            server = self._server()  # re-read for the post-start protocol
        self._check_protocol(server)
        self._ensured = True

    def _status(self) -> dict:
        """Parsed ``herdr status --json`` (rc=0 even when the server is down — a
        safe probe). Raises :class:`HerdrError` on a timeout / missing binary."""
        try:
            proc = self._run(["status", "--json"], check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr status failed: {exc}") from exc
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise HerdrError(f"herdr status returned non-JSON: {proc.stdout!r}") from exc
        return data if isinstance(data, dict) else {}

    def _server(self) -> dict:
        server = self._status().get("server")
        return server if isinstance(server, dict) else {}

    def _server_running(self) -> bool:
        return bool(self._server().get("running"))

    def _start_server(self) -> None:
        """Spawn a detached ``herdr server`` and poll until it reports running."""
        try:
            subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                ["herdr", "server"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **platform_util.detach_kwargs(),
            )
        except OSError as exc:
            raise HerdrError(f"could not start herdr server: {exc}") from exc
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._server_running():
                return
            time.sleep(_SERVER_POLL_S)
        raise HerdrError(f"herdr server did not report running within {SERVER_START_TIMEOUT_S}s")

    def _check_protocol(self, server: dict) -> None:
        """Fail below :data:`SUPPORTED_PROTOCOL`, warn once above. ``server`` is the
        ``.server`` object from a ``status --json`` read (``protocol`` is ``null``
        when down — read the SERVER's, not the binary-local ``api schema``)."""
        proto = server.get("protocol")
        if not isinstance(proto, int):
            return  # server down / unknown — the following op fails loudly on its own
        if proto < SUPPORTED_PROTOCOL:
            raise HerdrError(
                f"herdr server protocol {proto} < required {SUPPORTED_PROTOCOL}; upgrade herdr"
            )
        if proto > SUPPORTED_PROTOCOL and not self._proto_warned:
            self._proto_warned = True
            warnings.warn(
                f"herdr server protocol {proto} newer than tested {SUPPORTED_PROTOCOL}; "
                "proceeding but behavior is unverified",
                stacklevel=2,
            )


def _error_code(proc: subprocess.CompletedProcess[str]) -> str | None:
    """The server-answered error ``code`` from a non-zero result, or None when the
    failure was transport-level (non-JSON stderr == server down / unreachable).

    herdr's error bodies are ``{"error":{"code","message"},...}`` for most verbs
    and a bare ``{"code","message"}`` for ``pane read`` — both handled."""
    try:
        payload = json.loads((proc.stderr or "").strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    err = payload.get("error", payload)
    if isinstance(err, dict) and isinstance(err.get("code"), str):
        return err["code"]
    return None


# ------------------------------------------------------------- pipe_pane poller
#
# herdr has no `pipe-pane`/tee (nor any push stream on the CLI transport), so the
# tmux "append the pane's output to a log file" contract is emulated by polling
# `pane read` and appending a fresh snapshot whenever the pane's content changes.
# Two consumers read that log and MUST see it grow while a session is producing
# output: generic._log_activity_key (mtime,size) re-arms the dev-stall grace, and
# probe scans the log for completion markers. A #85-style no-op would leave the
# log flat and mis-stall a long silent-but-working turn.

_PANE_GONE = object()  # sentinel: `pane read` was answered with pane_not_found


class _PanePoller(threading.Thread):
    """A daemon thread that tees one herdr pane into a log file by polling.

    Every :data:`POLL_INTERVAL_S` it reads the pane's ``recent-unwrapped`` text
    (unwrapped so herdr's narrow default width can't split a marker across lines)
    and appends it to the log **only when the content changed** — content-hash
    gated, because the CLI ``revision`` is unusable (it stays 0 across both
    normal-buffer growth and alt-screen repaints; see the Phase-0 Findings). A
    static screen therefore stops growing the log, so a genuinely idle session
    can still stall; an actively-repainting one keeps re-arming the grace window.

    Retired by :meth:`stop` (kill_window / kill_session) or, on its own, after
    :data:`POLL_NOT_FOUND_LIMIT` consecutive server-answered ``pane_not_found``
    reads — the pane vanished when its process exited (Phase-0 O1). A transport
    hiccup (couldn't ask) is neither growth nor death: the tick is skipped and
    the not-found streak is left intact, exactly as the wait loop treats a probe
    error as "not proof of death"."""

    def __init__(
        self,
        client: _HerdrClient,
        pane_id: str,
        log_file: Path,
        *,
        interval_s: float | None = None,
        not_found_limit: int | None = None,
    ) -> None:
        super().__init__(daemon=True, name=f"herdr-poll-{pane_id}")
        self._client = client
        self._pane_id = pane_id
        self._log_file = Path(log_file)
        # Resolve the cadence from the module globals at construction (not as
        # signature defaults) so a test can shrink POLL_* before starting one.
        self._interval_s = POLL_INTERVAL_S if interval_s is None else interval_s
        self._not_found_limit = POLL_NOT_FOUND_LIMIT if not_found_limit is None else not_found_limit
        self._stop_event = threading.Event()
        self._last_hash: str | None = None

    def stop(self) -> None:
        """Signal the thread to exit. Returns immediately: the event wakes the
        interval sleep at once, but an in-flight ``pane read`` still finishes
        first (the thread is a daemon, so a hung read never blocks shutdown)."""
        self._stop_event.set()

    def prime(self) -> bool:
        """Do one synchronous read before the thread starts. Returns True (and
        logs the first snapshot) if the pane answered with text; False if it is
        already gone or unreachable — the caller then declines to spin up a
        thread, which is how :meth:`HerdrMultiplexer.pipe_pane` stays tolerant of
        a pane that died on launch (probe.py depends on that tolerance)."""
        snapshot = self._read_snapshot()
        if isinstance(snapshot, str):
            self._record(snapshot)
            return True
        return False

    def run(self) -> None:
        not_found = 0
        # wait() returns True the instant stop() fires (-> exit) or False on
        # timeout (-> poll). prime() already captured t0, so the first poll is
        # one interval in — no immediate re-read.
        while not self._stop_event.wait(self._interval_s):
            snapshot = self._read_snapshot()
            if snapshot is _PANE_GONE:
                not_found += 1
                if not_found >= self._not_found_limit:
                    return
            elif isinstance(snapshot, str):
                not_found = 0
                self._record(snapshot)
            # else: transport hiccup — skip this tick, keep the not-found streak.

    def _read_snapshot(self) -> str | object | None:
        """One ``pane read``: the raw pane text (str), :data:`_PANE_GONE` when the
        server answered ``pane_not_found``, or None on a transport failure."""
        try:
            proc = self._client._run(
                ["pane", "read", self._pane_id, "--source", "recent-unwrapped"],
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if proc.returncode == 0:
            return proc.stdout
        if _error_code(proc) == "pane_not_found":
            return _PANE_GONE
        return None  # non-JSON `Error: Os` etc. — unreachable, retry next tick

    def _record(self, text: str) -> None:
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        if digest == self._last_hash:
            return  # unchanged screen: not activity, don't grow the log
        self._last_hash = digest
        if text.strip():  # a blank repaint isn't worth a log line
            self._append(text)

    def _append(self, text: str) -> None:
        # Append-only so the log's inode/size grow monotonically (the activity
        # signal). A write failure must never crash the tee thread.
        if not text.endswith("\n"):
            text += "\n"
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            with self._log_file.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass


class HerdrMultiplexer(TerminalMultiplexer):
    """herdr backend implementing the full :class:`TerminalMultiplexer` contract.

    The constructor does **no I/O** — ``detect_multiplexers`` instantiates every
    registered backend, so ``available()`` (a plain ``shutil.which``) is the only
    host probe, and the server is only ever touched by the mutating ops via
    :meth:`_HerdrClient.ensure_server`."""

    def __init__(self) -> None:
        self._client = _HerdrClient()
        # Live pipe_pane tees, keyed by native window id (== pane id). Mutated
        # only from the caller's thread (pipe_pane / kill_*); the poller threads
        # never touch it. The lock is defensive hygiene, not a hot path.
        self._pollers: dict[str, _PanePoller] = {}
        self._pollers_lock = threading.Lock()

    # -------------------------------------------------- enumeration helpers

    def _list_workspaces_strict(self) -> list[dict]:
        """Every workspace; raises on ANY failure (transport or server-down).

        This is the liveness-honest enumeration: a successful ``workspace list``
        proves the server answered, so an absent label is honestly "no such
        workspace", never "couldn't ask". ``workspace list`` has no not-found
        case, so any non-zero exit is a transport failure -> raise."""
        try:
            proc = self._client._run(["workspace", "list"], check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr workspace list failed: {exc}") from exc
        if proc.returncode != 0:
            raise HerdrError(f"herdr workspace list failed: {proc.stderr.strip()}")
        return _envelope_items(proc.stdout, "workspaces", strict=True)

    def _list_workspaces_tolerant(self) -> list[dict] | None:
        """Every workspace, or None when the query could not be answered (herdr
        missing / server down / transport failure). Used by the diagnostic
        sentinels, which must never start a server or raise."""
        if not shutil.which("herdr"):
            return None
        try:
            proc = self._client._run(["workspace", "list"], check=False)
        except (subprocess.SubprocessError, OSError):
            return None
        if proc.returncode != 0:
            return None
        return _envelope_items(proc.stdout, "workspaces", strict=False)

    def _list_panes_strict(self) -> list[dict]:
        """Every pane across all workspaces; raises on failure. Never
        ``pane list --workspace <maybe-absent>`` — that RAISES ``workspace_not_found``
        rather than returning [], so we enumerate all panes and filter in-process."""
        try:
            proc = self._client._run(["pane", "list"], check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr pane list failed: {exc}") from exc
        if proc.returncode != 0:
            raise HerdrError(f"herdr pane list failed: {proc.stderr.strip()}")
        return _envelope_items(proc.stdout, "panes", strict=True)

    def _list_tabs_strict(self) -> list[dict]:
        """Every tab across all workspaces; raises on failure. Never
        ``tab list --workspace <maybe-absent>`` — that RAISES ``workspace_not_found``
        (the pane-list trap again); enumerate all tabs and filter in-process."""
        try:
            proc = self._client._run(["tab", "list"], check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr tab list failed: {exc}") from exc
        if proc.returncode != 0:
            raise HerdrError(f"herdr tab list failed: {proc.stderr.strip()}")
        return _envelope_items(proc.stdout, "tabs", strict=True)

    def _list_tabs_tolerant(self) -> list[dict] | None:
        """Every tab, or None when the query could not be answered."""
        if not shutil.which("herdr"):
            return None
        try:
            proc = self._client._run(["tab", "list"], check=False)
        except (subprocess.SubprocessError, OSError):
            return None
        if proc.returncode != 0:
            return None
        return _envelope_items(proc.stdout, "tabs", strict=False)

    def _workspace_row(self, label: str, *, strict: bool) -> dict | None:
        """First-match resolution of a session name to its workspace row. herdr
        allows DUPLICATE labels (tmux session names are unique; herdr's are not),
        so callers must tolerate more than one and take the first."""
        workspaces = self._list_workspaces_strict() if strict else self._list_workspaces_tolerant()
        if not workspaces:
            return None
        for ws in workspaces:
            if ws.get("label") == label:
                return ws if isinstance(ws.get("workspace_id"), str) else None
        return None

    def _workspace_id(self, label: str, *, strict: bool) -> str | None:
        row = self._workspace_row(label, strict=strict)
        return row["workspace_id"] if row is not None else None

    def _tab_root_pane(self, tab_id: str, *, strict: bool) -> str | None:
        """The tab's first pane — bmad-loop windows are single-pane tabs, so this
        is the pane that IS the window."""
        try:
            panes = self._list_panes_strict()
        except MultiplexerError:
            if strict:
                raise
            return None
        for pane in panes:
            if pane.get("tab_id") == tab_id and isinstance(pane.get("pane_id"), str):
                return pane["pane_id"]
        return None

    def _parse_target(self, target: str, *, strict: bool) -> str | None:
        """Resolve any window target to a native pane id.

        Callers hand this two families: the seam-canonical ``=session[:window]``
        token (decoded by :func:`multiplexer.parse_target`; core formats these
        via ``TerminalMultiplexer.target``) and our own native pane id — native
        ids contain ``:`` (``w1:p1``) but never lead with ``=``. A native id
        passes through untouched, with no server round-trip. For ``=…`` targets:
        session → workspace (first-match label), a window name → the tab with
        that label, no window → the session-level tab (see
        :func:`_session_level_tab`), then that tab's pane. ``strict=True``
        raises :class:`HerdrError` when the target cannot be resolved;
        ``strict=False`` returns None (the sentinel callers' quiet failure)."""
        parsed = parse_target(target)
        if parsed is None:
            return target
        session, window = parsed
        row = self._workspace_row(session, strict=strict)
        if row is None:
            if strict:
                raise HerdrError(f"herdr workspace for session {session!r} not found")
            return None
        wid = row["workspace_id"]
        tabs = self._list_tabs_strict() if strict else self._list_tabs_tolerant()
        tabs = [
            t
            for t in tabs or []
            if t.get("workspace_id") == wid and isinstance(t.get("tab_id"), str)
        ]
        if window:
            tab = next((t for t in tabs if t.get("label") == window), None)
        else:
            tab = _session_level_tab(tabs, row.get("active_tab_id"))
        if tab is None:
            if strict:
                raise HerdrError(f"herdr target {target!r} matches no tab")
            return None
        pane_id = self._tab_root_pane(tab["tab_id"], strict=strict)
        if pane_id is None and strict:
            raise HerdrError(f"herdr tab {tab['tab_id']!r} for target {target!r} has no pane")
        return pane_id

    # ------------------------------------------------------------ sessions

    def has_session(self, name: str) -> bool:
        # Ensures the server first: this gates session creation, so a fresh run
        # that finds the server down must bring it up to answer authoritatively
        # (and to then create the workspace). A transport failure raises.
        self._client.ensure_server()
        return self._workspace_id(name, strict=True) is not None

    def new_session(
        self, name: str, cwd: Path, cols: int | None = None, lines: int | None = None
    ) -> None:
        # Workspace create auto-spawns a root shell tab, which keeps the workspace
        # alive after task tabs close (tmux window-0 role). Geometry is advisory:
        # herdr has no absolute headless resize, so cols/lines are ignored here.
        self._client.ensure_server()
        # herdr allows duplicate labels; guard so a re-armed/resumed run does not
        # spawn a second workspace with the same name.
        if self._workspace_id(name, strict=True) is not None:
            return
        self._client._herdr_json(
            "workspace", "create", "--label", name, "--cwd", str(cwd), "--no-focus"
        )

    def kill_session(self, name: str) -> None:
        # Best-effort teardown: never start a server just to tear one down, and
        # tolerate the workspace already being gone. Also retire the session's
        # pipe_pane tees so no poller outlives the workspace it was watching.
        wid: str | None = None
        if shutil.which("herdr"):
            try:
                wid = self._workspace_id(name, strict=False)
                if wid is not None:
                    self._client._run(["workspace", "close", wid], check=False)
            except (subprocess.SubprocessError, OSError):
                wid = None
        if wid is not None:
            self._stop_pollers_for_workspace(wid)
            _drop_windows_for_workspace(wid)
        _drop_state("sessions", name)

    def list_sessions(self) -> list[str]:
        # [] when herdr is missing, no server is running, or the query fails —
        # indistinguishable and all "nothing live", as with tmux list-sessions.
        workspaces = self._list_workspaces_tolerant()
        if workspaces is None:
            return []
        return [ws["label"] for ws in workspaces if isinstance(ws.get("label"), str)]

    def session_options(self, option: str) -> dict[str, str]:
        # Map live-session name -> sidecar option value. Also prunes sidecar
        # entries whose workspace is gone (only when liveness is knowable — a
        # None enumeration can't prove a workspace dead, so it prunes nothing).
        workspaces = self._list_workspaces_tolerant()
        if workspaces is None:
            return {}
        labels = {ws["label"] for ws in workspaces if isinstance(ws.get("label"), str)}
        # Lock-free read for the answer (atomic_replace guarantees a consistent
        # snapshot; readers never block on writers)...
        sessions = _load_state()["sessions"]
        result = {
            label: sessions[label][option]
            for label in labels
            if label in sessions and option in sessions[label]
        }
        dead = [key for key in sessions if key not in labels]
        if dead:
            # ...then the prune is a proper locked read-modify-write (re-loaded
            # under the lock so it never clobbers a concurrent writer's update).
            try:
                with _state_lock():
                    state = _load_state()
                    pruned = False
                    for key in dead:
                        if state["sessions"].pop(key, None) is not None:
                            pruned = True
                    if pruned:
                        _save_state(state)
            except OSError:
                pass  # best-effort prune
        return result

    def set_session_option(self, name: str, option: str, value: str) -> None:
        # Raiser: a sidecar write failure is this backend's "transport failure"
        # (a lock-acquisition failure counts — never write unguarded).
        try:
            with _state_lock():
                state = _load_state()
                state["sessions"].setdefault(name, {})[option] = value
                _save_state(state)
        except OSError as exc:
            raise HerdrError(f"herdr sidecar write failed: {exc}") from exc

    # ------------------------------------------------------------- windows

    def new_window(
        self, session: str, name: str, cwd: Path, env: dict[str, str], command: str
    ) -> str:
        # A window is a tab with a single pane; the native window id we hand back
        # is that pane's id. The command (a shlex-joined argv on EVERY platform,
        # per the contract — core never win32-quotes) is re-split and launched so
        # process exit == pane close == tab close == tmux-identical death
        # semantics: via a typed `exec` on POSIX, via `agent start` on win32.
        self._client.ensure_server()
        wid = self._workspace_id(session, strict=True)
        if wid is None:
            raise HerdrError(f"herdr workspace for session {session!r} not found")
        if _is_win32():
            return self._new_window_win32(wid, name, cwd, env, shlex.split(command))
        argv: list[str] = ["tab", "create", "--workspace", wid, "--label", name, "--cwd", str(cwd)]
        for key, val in env.items():
            argv += ["--env", f"{key}={val}"]
        argv.append("--no-focus")
        result = self._client._herdr_json(*argv)
        pane_id = _root_pane_id(result)
        if pane_id is None:
            raise HerdrError(f"herdr tab create did not return a root pane id: {result!r}")
        self._launch(pane_id, shlex.split(command))
        return pane_id

    def _launch(self, pane_id: str, argv: list[str]) -> None:
        # `pane run` types the line and presses Enter atomically. `exec` replaces
        # the shell so the process IS the pane; POSIX-only by design — win32
        # launches never reach here (_new_window_win32's agent start is the
        # equivalent, spawning argv with no shell to replace).
        self._client._herdr("pane", "run", pane_id, "exec " + shlex.join(argv))

    def _agent_start(
        self,
        name: str,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        tab_id: str,
    ) -> dict:
        """One ``agent start`` — the win32 launch verb: herdr spawns ``argv``
        DIRECTLY as a new pane's process (no shell, so process-exit ==
        pane-close without POSIX ``exec``). Returns the AgentInfo dict; raises
        :class:`HerdrError` when the envelope lacks the pane identity the
        caller is about to hand back.

        Agent names are SERVER-global-unique while their pane lives (Phase-A
        E8) — two concurrent runs both have a probe window, so the display name
        is uniquified with the tab id (server-unique, stateless, and
        sidebar-diagnosable; nothing ever targets BY agent name — pane ids and
        tab labels carry targeting)."""
        args: list[str] = ["agent", "start", f"{name}@{tab_id}", "--cwd", str(cwd)]
        args += ["--tab", tab_id]
        for key, val in (env or {}).items():
            args += ["--env", f"{key}={val}"]
        args += ["--no-focus", "--", *argv]
        result = self._client._herdr_json(*args)
        agent = result.get("agent")
        if not isinstance(agent, dict) or not isinstance(agent.get("pane_id"), str):
            raise HerdrError(f"herdr agent start did not return an agent pane: {result!r}")
        return agent

    def _new_window_win32(
        self, wid: str, name: str, cwd: Path, env: dict[str, str], argv: list[str]
    ) -> str:
        # Win32 has no `exec`, so a typed launch would leave the (unknown)
        # default shell as the pane process and break exit==death. Instead:
        # create the tab (its label IS the window name that `=session:window`
        # resolution keys on — `agent start` can only split, never create a
        # tab, Phase-A E1), split the agent's process into it, then close the
        # tab's bootstrap shell pane so the tab is single-pane again (E5). The
        # close is STRICT: a lingering shell would be the tab's FIRST pane
        # (shadowing _parse_target's root-pane resolution) and a phantom
        # immortal window in list_window_ids. Env rides `agent start` — the
        # agent process is what needs it; the doomed bootstrap shell gets none.
        result = self._client._herdr_json(
            "tab", "create", "--workspace", wid, "--label", name, "--cwd", str(cwd), "--no-focus"
        )
        shell_pane, tab_id = _root_pane_id(result), _tab_id(result)
        if shell_pane is None or tab_id is None:
            raise HerdrError(f"herdr tab create did not return a tab/root pane: {result!r}")
        agent_pane: str | None = None
        try:
            agent = self._agent_start(name, argv, cwd=cwd, env=env, tab_id=tab_id)
            agent_pane = agent["pane_id"]
            self._client._herdr("pane", "close", shell_pane)
        except MultiplexerError:
            self._rollback_win32_launch(shell_pane, agent_pane)
            raise
        return agent_pane

    def _rollback_win32_launch(self, shell_pane: str, agent_pane: str | None) -> None:
        """Best-effort teardown of a half-built win32 window (tab create
        succeeded, a later step failed). POSIX new_window has no rollback — its
        failure leaves a benign idle tab; the win32 failure modes leave a
        harmful TWO-pane tab (a phantom window per pane), so both panes go.
        kill_window on the agent pane also drops its sidecar entry + return
        file; the shell-pane close cascades the emptied tab."""
        if agent_pane is not None:
            try:
                self.kill_window(agent_pane)
            except MultiplexerError:
                pass
        if shutil.which("herdr"):
            try:
                self._client._run(["pane", "close", shell_pane], check=False)
            except (subprocess.SubprocessError, OSError):
                pass

    def new_parked_window(
        self, session: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        # A fresh tab whose recipe runs argv, echoes the exit banner, parks on a
        # blocking read (the exit status stays inspectable), and finally hands
        # an attached client back to its origin via the per-window return file
        # (see _parked_source / the module docstring's ledger). On POSIX the
        # recipe is a typed `exec sh -c '<recipe>'` — exec replaces the shell,
        # so finishing the source closes the pane, which is also what ends a
        # watching `terminal attach` client; win32 rides `agent start` instead
        # (_new_parked_window_win32).
        self._client.ensure_server()
        wid = self._workspace_id(session, strict=True)
        if wid is None:
            raise HerdrError(f"herdr workspace for session {session!r} not found")
        if _is_win32():
            return self._new_parked_window_win32(wid, name, cwd, argv, return_opt)
        result = self._client._herdr_json(
            "tab", "create", "--workspace", wid, "--label", name, "--cwd", str(cwd), "--no-focus"
        )
        pane_id = _root_pane_id(result)
        if pane_id is None:
            raise HerdrError(f"herdr tab create did not return a root pane id: {result!r}")
        # Record which option this window's trailer consumes BEFORE typing the
        # recipe, so a set_window_option racing the launch already mirrors into
        # the return file. tmux gets create+launch atomically (one new-window
        # call); here the tab already exists, so any failure before the recipe
        # is typed must roll it back or it lingers as an untracked idle shell
        # until the whole workspace is closed.
        try:
            try:
                with _state_lock():
                    state = _load_state()
                    state["windows"].setdefault(pane_id, {})[_PARKED_RETURN_KEY] = return_opt
                    _save_state(state)
            except OSError as exc:
                raise HerdrError(f"herdr sidecar write failed: {exc}") from exc
            self._client._herdr(
                "pane", "run", pane_id, "exec sh -c " + shlex.quote(_parked_source(argv, pane_id))
            )
        except MultiplexerError:
            try:
                self.kill_window(pane_id)
            except MultiplexerError:
                pass
            raise
        return pane_id

    def _new_parked_window_win32(
        self, wid: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        # The win32 parked window rides the same tab-create -> agent-start ->
        # shell-close shape as _new_window_win32, with the agent process being
        # a PowerShell host running the pwsh recipe (one argv element — no
        # typed-through-default-shell quoting layer exists on win32, which is
        # exactly why agent start is the launch verb). When the recipe's final
        # statement ends, the PowerShell process exits -> pane closes -> tab
        # cascades -> a blocking `terminal attach` client exits: the POSIX
        # post-exit chain, verified for agent-started panes in Phase-A E9.
        #
        # ORDERING DEVIATION vs POSIX: there the _PARKED_RETURN_KEY sidecar
        # write precedes the recipe launch (the pane id exists first); here the
        # window id IS the launch's product, so the write happens immediately
        # AFTER agent start — still before this method returns (no caller can
        # hold the native id earlier) and human-scale before the trailer reads
        # the return file at park-exit. Residual: a "=session:name"-target
        # set_window_option racing the create->write window resolves to the
        # doomed bootstrap shell pane and mirrors nothing — bounded, because
        # launch.set_return_pane fires at attach time, not launch time.
        recipe_argv = [_pwsh_binary(), "-NoProfile", "-Command", _parked_source_pwsh(argv)]
        result = self._client._herdr_json(
            "tab", "create", "--workspace", wid, "--label", name, "--cwd", str(cwd), "--no-focus"
        )
        shell_pane, tab_id = _root_pane_id(result), _tab_id(result)
        if shell_pane is None or tab_id is None:
            raise HerdrError(f"herdr tab create did not return a tab/root pane: {result!r}")
        agent_pane: str | None = None
        try:
            agent = self._agent_start(name, recipe_argv, cwd=cwd, tab_id=tab_id)
            agent_pane = agent["pane_id"]
            try:
                with _state_lock():
                    state = _load_state()
                    state["windows"].setdefault(agent_pane, {})[_PARKED_RETURN_KEY] = return_opt
                    _save_state(state)
            except OSError as exc:
                raise HerdrError(f"herdr sidecar write failed: {exc}") from exc
            self._client._herdr("pane", "close", shell_pane)
        except MultiplexerError:
            self._rollback_win32_launch(shell_pane, agent_pane)
            raise
        return agent_pane

    def list_window_ids(self, session: str) -> list[str]:
        # The engine's liveness probe: [] means "no windows", and a transport
        # failure must RAISE (never []), so a mere server hang can't read as
        # "window dead -> crashed". Does NOT ensure_server: a liveness check must
        # not resurrect a dead server; an unreachable server is honestly "unknown"
        # -> raise. An absent workspace, though, is a knowable "no windows" -> [].
        wid = self._workspace_id(session, strict=True)
        if wid is None:
            return []
        return [
            pane["pane_id"]
            for pane in self._list_panes_strict()
            if pane.get("workspace_id") == wid and isinstance(pane.get("pane_id"), str)
        ]

    def list_windows(self, session: str, fields: list[str]) -> list[tuple[str, ...]]:
        # Best-effort metadata (sentinel [] on any failure). The tmux format
        # fields tui/launch.py asks for are mapped onto the herdr object model
        # (window == tab, native window id == the tab's root pane id):
        # window_id/pane_id -> pane_id, window_name -> the owning tab's label,
        # session_name -> the session, "@..." user options -> the sidecar.
        # Anything unmapped falls back to the pane dict's own herdr-native key.
        wid = self._workspace_id(session, strict=False)
        if wid is None:
            return []
        try:
            panes = self._list_panes_strict()
        except MultiplexerError:
            return []
        tab_labels: dict[str, str] = {}
        if "window_name" in fields:
            tabs = self._list_tabs_tolerant()
            if tabs is None:
                return []  # can't answer a requested field -> the [] sentinel
            for tab in tabs:
                if isinstance(tab.get("tab_id"), str):
                    label = tab.get("label")
                    tab_labels[tab["tab_id"]] = label if isinstance(label, str) else ""
        options: dict = {}
        if any(field.startswith("@") for field in fields):
            options = _load_state()["windows"]
        rows: list[tuple[str, ...]] = []
        for pane in panes:
            if pane.get("workspace_id") != wid:
                continue
            pane_id = pane.get("pane_id")
            pane_id = pane_id if isinstance(pane_id, str) else ""
            row: list[str] = []
            for field in fields:
                if field in ("window_id", "pane_id"):
                    row.append(pane_id)
                elif field == "window_name":
                    tab_id = pane.get("tab_id")
                    row.append(tab_labels.get(tab_id, "") if isinstance(tab_id, str) else "")
                elif field == "session_name":
                    row.append(session)
                elif field.startswith("@"):
                    opts = options.get(pane_id, {})
                    value = opts.get(field, "") if isinstance(opts, dict) else ""
                    row.append(value if isinstance(value, str) else "")
                else:
                    row.append(str(pane.get(field, "")))
            rows.append(tuple(row))
        return rows

    def window_alive(self, session: str, window_id: str) -> bool:
        # Single-signal liveness: server-answered pane presence. No linger was
        # observed for the exec launch (the pane vanishes on exit), so pane
        # presence alone is authoritative. May raise (transport unknowable).
        return self._pane_counts_as_live(window_id)

    def _pane_counts_as_live(self, pane_id: str) -> bool:
        try:
            proc = self._client._run(["pane", "get", pane_id], check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr pane get failed: {exc}") from exc
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise HerdrError(f"herdr pane get returned non-JSON: {proc.stdout!r}") from exc
            result = data.get("result") if isinstance(data, dict) else None
            return isinstance(result, dict) and "pane" in result
        # Non-zero: a server-answered pane_not_found is honestly "dead"; a
        # non-JSON transport error (server down / bogus socket) is unknowable.
        if _error_code(proc) == "pane_not_found":
            return False
        raise HerdrError(f"herdr pane get failed: {proc.stderr.strip()}")

    def kill_window(self, target: str) -> None:
        # Best-effort: `pane close` cascades the now-empty tab closed; tolerate the
        # pane already being gone (a CLI that crashes on launch races its window
        # down before teardown — probe.py depends on this tolerance).
        pane_id = self._parse_target(target, strict=False)
        if pane_id is None:
            return
        self._stop_poller(pane_id)
        if shutil.which("herdr"):
            try:
                self._client._run(["pane", "close", pane_id], check=False)
            except (subprocess.SubprocessError, OSError):
                pass
        _drop_state("windows", pane_id)
        _remove_return_file(pane_id)

    def select_window(self, target: str) -> None:
        # Best-effort focus: resolve the pane's tab and focus it (herdr has
        # `tab focus`, not `pane focus`). Swallow any failure to the no-op sentinel.
        if not shutil.which("herdr"):
            return
        pane_id = self._parse_target(target, strict=False)
        if pane_id is None:
            return
        try:
            proc = self._client._run(["pane", "get", pane_id], check=False)
            if proc.returncode != 0:
                return
            pane = json.loads(proc.stdout).get("result", {}).get("pane", {})
            tab_id = pane.get("tab_id")
            if isinstance(tab_id, str) and tab_id:
                self._client._run(["tab", "focus", tab_id], check=False)
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            pass

    def set_window_option(self, target: str, option: str, value: str) -> None:
        # Best-effort sidecar write (sentinel: swallow OSError to a no-op — a
        # lock failure skips the write entirely, never writes unguarded). Keys
        # are normalized to the native pane id, so a write by tmux-style
        # "=session:name" target (set_return_pane) and a read by native id
        # (return_attached_client) agree.
        pane_id = self._parse_target(target, strict=False)
        if pane_id is None:
            return
        parked_opt = None
        try:
            with _state_lock():
                state = _load_state()
                opts = state["windows"].setdefault(pane_id, {})
                opts[option] = value
                parked_opt = opts.get(_PARKED_RETURN_KEY)
                _save_state(state)
        except OSError:
            return
        if parked_opt == option:
            self._mirror_return_value(pane_id, value)

    def _mirror_return_value(self, pane_id: str, value: str) -> None:
        """Mirror a parked window's return-option value into its return file, in
        trailer-actionable form: :data:`PARKED_RETURN_DETACH` verbatim; a pane id
        is resolved to its TAB id here, where JSON parsing is cheap, so the sh
        trailer stays a dumb ``tab focus``. Best-effort — an unresolvable origin
        clears the file instead, so a stale target can't yank focus somewhere
        wrong later."""
        if value == PARKED_RETURN_DETACH:
            _write_return_file(pane_id, value)
            return
        tab_id: str | None = None
        try:
            candidate = self._client._herdr_json("pane", "get", value).get("pane", {})
            if isinstance(candidate, dict) and isinstance(candidate.get("tab_id"), str):
                tab_id = candidate["tab_id"] or None
        except MultiplexerError:
            tab_id = None
        if tab_id is None:
            _remove_return_file(pane_id)
        else:
            _write_return_file(pane_id, tab_id)

    def unset_window_option(self, target: str, option: str) -> None:
        pane_id = self._parse_target(target, strict=False)
        if pane_id is None:
            return
        parked_opt = None
        try:
            with _state_lock():
                state = _load_state()
                opts = state["windows"].get(pane_id)
                if isinstance(opts, dict) and option in opts:
                    del opts[option]
                    parked_opt = opts.get(_PARKED_RETURN_KEY)
                    if not opts:
                        del state["windows"][pane_id]
                    _save_state(state)
        except OSError:
            return
        if parked_opt == option:
            _remove_return_file(pane_id)

    def show_window_option(self, target: str, option: str) -> str:
        # "" reads as "unset" — also the failure sentinel (including a "=..."
        # target that no longer resolves). Keys normalized like set_window_option.
        pane_id = self._parse_target(target, strict=False)
        if pane_id is None:
            return ""
        opts = _load_state()["windows"].get(pane_id, {})
        value = opts.get(option, "") if isinstance(opts, dict) else ""
        return value if isinstance(value, str) else ""

    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        # Emulate tmux `pipe-pane` with a per-window polling tee (see _PanePoller).
        # Sentinel contract: never raise. A prime() read that fails means the pane
        # already died on launch (or the server is unreachable) — mirror tmux
        # swallowing that race by simply not starting a tee; the dead window is
        # then reported as a crash by wait_for_completion.
        if not shutil.which("herdr"):
            return None
        poller = _PanePoller(self._client, window_id, Path(log_file))
        if not poller.prime():
            return None
        with self._pollers_lock:
            previous = self._pollers.pop(window_id, None)
            self._pollers[window_id] = poller
        if previous is not None:  # a re-armed window replaces its old tee
            previous.stop()
        poller.start()
        return None

    def _stop_poller(self, window_id: str) -> None:
        with self._pollers_lock:
            poller = self._pollers.pop(window_id, None)
        if poller is not None:
            poller.stop()

    def _stop_pollers_for_workspace(self, workspace_id: str) -> None:
        # A pane id is `<workspace_id>:p<n>`, so the prefix identifies the session's
        # tees without re-querying herdr. (A poller whose pane merely vanished
        # would also self-retire on not-founds; this just frees it promptly.)
        with self._pollers_lock:
            doomed = [pid for pid in self._pollers if pid.split(":", 1)[0] == workspace_id]
            pollers = [self._pollers.pop(pid) for pid in doomed]
        for poller in pollers:
            poller.stop()

    def send_text(self, window_id: str, text: str) -> None:
        # Literal paste, let the TUI ingest it, then submit — the tmux
        # send-text / sleep / Enter ordering, in herdr verbs (Enter is lowercase).
        self._client._herdr("pane", "send-text", window_id, text)
        time.sleep(0.3)
        self._client._herdr("pane", "send-keys", window_id, "enter")

    # ----------------------------------------------------- client / attach

    def attach_target_argv(self, target: str) -> list[str]:
        # Resolve either target family (native pane id or a tmux-style
        # "=session[:window]" spec — see _parse_target) to its pane, then:
        #   - inside a herdr pane (HERDR_ENV): nesting a full attach is the
        #     wrong move, exactly like attach-inside-tmux — return the
        #     fire-and-forget `tab focus`, the switch-client equivalent;
        #   - outside: attach the caller's terminal to the pane's terminal_id
        #     (`herdr terminal attach` blocks, and exits when the pane closes).
        try:
            pane_id = self._parse_target(target, strict=True)
            if pane_id is None:  # strict never returns None; belt and braces
                raise HerdrError("target resolves to no pane")
            result = self._client._herdr_json("pane", "get", pane_id)
        except HerdrError as exc:
            raise HerdrError(
                f"cannot resolve a herdr terminal for {target!r} to attach: {exc}"
            ) from exc
        pane = result.get("pane")
        pane = pane if isinstance(pane, dict) else {}
        if os.environ.get(_HERDR_ENV_MARKER) == "1":
            tab_id = pane.get("tab_id")
            if isinstance(tab_id, str) and tab_id:
                return ["herdr", "tab", "focus", tab_id]
            raise HerdrError(f"herdr pane {pane_id!r} has no tab to focus")
        terminal_id = pane.get("terminal_id")
        if not isinstance(terminal_id, str) or not terminal_id:
            raise HerdrError(f"herdr pane {pane_id!r} has no terminal to attach")
        return ["herdr", "terminal", "attach", terminal_id]

    def current_pane_id(self) -> str | None:
        return self._current_from_env("HERDR_PANE_ID")

    def current_window_id(self) -> str | None:
        # Our native window id is the tab's root pane id; inside a single-pane
        # bmad-loop window the current pane IS that root pane.
        return self._current_from_env("HERDR_PANE_ID")

    def current_session(self) -> str | None:
        # Resolve the injected workspace id back to its label (the session name);
        # best-effort, None when not inside herdr or the label can't be resolved.
        if os.environ.get(_HERDR_ENV_MARKER) != "1":
            return None
        ws_id = os.environ.get("HERDR_WORKSPACE_ID")
        if not ws_id:
            return None
        workspaces = self._list_workspaces_tolerant() or []
        for ws in workspaces:
            if ws.get("workspace_id") == ws_id and isinstance(ws.get("label"), str):
                return ws["label"]
        return None

    def _current_from_env(self, key: str) -> str | None:
        if os.environ.get(_HERDR_ENV_MARKER) != "1":
            return None
        value = os.environ.get(key)
        return value or None

    def detach_client(self) -> None:
        # herdr detach is a keybinding, with no CLI verb — documented no-op.
        return None

    def switch_client(self, target: str, last_fallback: bool = False) -> bool:
        # The herdr "switch client" move is a tab focus: focusing a tab also
        # flips workspace focus when it lives elsewhere (verified 0.7.3), so one
        # verb covers the whole return-to-origin hop. True iff the focus landed.
        # herdr has no "last client" concept, so last_fallback has nothing to
        # fall back to and a failed switch is honestly False.
        pane_id = self._parse_target(target, strict=False)
        if pane_id is None:
            return False
        try:
            pane = self._client._herdr_json("pane", "get", pane_id).get("pane", {})
        except MultiplexerError:
            return False
        tab_id = pane.get("tab_id") if isinstance(pane, dict) else None
        if not isinstance(tab_id, str) or not tab_id:
            return False
        try:
            proc = self._client._run(["tab", "focus", tab_id], check=False)
        except (subprocess.SubprocessError, OSError):
            return False
        return proc.returncode == 0

    def available(self) -> bool:
        # A plain PATH probe — NEVER touches the server (detect_multiplexers
        # instantiates every backend and must stay side-effect-free).
        return shutil.which("herdr") is not None

    def version(self) -> str | None:
        if not shutil.which("herdr"):
            return None
        try:
            return self._client._herdr("--version")
        except (MultiplexerError, subprocess.SubprocessError, OSError):
            return None


# --------------------------------------------------------------- parse helpers


def _envelope_items(stdout: str, key: str, *, strict: bool) -> list[dict]:
    """Extract ``result.<key>`` (a list of dicts) from a herdr JSON envelope. In
    strict mode a non-JSON body raises :class:`HerdrError`; otherwise it yields []."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        if strict:
            raise HerdrError(f"herdr returned non-JSON: {stdout!r}") from exc
        return []
    result = data.get("result") if isinstance(data, dict) else None
    items = result.get(key) if isinstance(result, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _session_level_tab(tabs: list[dict], active_tab_id: object) -> dict | None:
    """The tab a session-level (``=session``) target lands on: the workspace's
    active tab — except when that is the root shell tab and task tabs exist,
    where the NEWEST tab wins. Rationale: ``new_window``/``new_parked_window``
    create tabs ``--no-focus`` (never yanking an attached client's focus), so a
    headless agent workspace keeps its root shell active while the window the
    caller wants is the newest tab; the ctl session is unaffected because
    ``attach_plan`` runs ``select_window`` (a ``tab focus``) first, making the
    intended window the active one."""
    if not tabs:
        return None

    def num(tab: dict) -> int:
        n = tab.get("number")
        return n if isinstance(n, int) else 0

    root = min(tabs, key=num)
    active = next((t for t in tabs if t.get("tab_id") == active_tab_id), None)
    if active is not None and active.get("tab_id") != root.get("tab_id"):
        return active
    return max(tabs, key=num)  # root active (or unknown): newest tab (== root when alone)


def _root_pane_id(result: dict) -> str | None:
    root = result.get("root_pane")
    if isinstance(root, dict) and isinstance(root.get("pane_id"), str):
        return root["pane_id"]
    return None


def _tab_id(result: dict) -> str | None:
    tab = result.get("tab")
    if isinstance(tab, dict) and isinstance(tab.get("tab_id"), str):
        return tab["tab_id"]
    return None


def _drop_state(section: str, key: str) -> None:
    """Best-effort removal of one sidecar entry (a workspace/window gone). Never
    raises — a teardown/kill must not fail on a sidecar hiccup."""
    try:
        with _state_lock():
            state = _load_state()
            if key in state.get(section, {}):
                del state[section][key]
                _save_state(state)
    except OSError:
        pass


def _drop_windows_for_workspace(workspace_id: str) -> None:
    """Best-effort sidecar + return-file cleanup for every window of a closed
    workspace (pane ids are ``<workspace_id>:p<n>`` — the same prefix rule the
    poller registry uses). Never raises."""
    prefix = workspace_id + ":"
    doomed: list[str] = []
    try:
        with _state_lock():
            state = _load_state()
            doomed = [key for key in state["windows"] if key.startswith(prefix)]
            for key in doomed:
                del state["windows"][key]
            if doomed:
                _save_state(state)
    except OSError:
        return  # unknown keys: the trailers' own `rm -f` self-cleans the files
    for key in doomed:
        _remove_return_file(key)


# Self-registration: importing this module (directly, or via bmad-loop's
# ``bmad_loop.mux_backends`` entry-point scan) makes the backend selectable.
# `matches` is True on every platform — herdr itself is cross-platform, and
# launches are native on both families (typed exec on POSIX, agent start on
# win32); whether it is *usable* here is `available()`'s PATH probe.
# Registration is name-keyed first-wins, so a bundled backend of the same name
# cannot shadow this one once this module is imported first.
register_multiplexer("herdr", lambda platform: True, HerdrMultiplexer)
