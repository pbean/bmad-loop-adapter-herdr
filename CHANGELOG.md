# Changelog

## 0.4.0 — unreleased

Tracks bmad-loop main past 0.9.0 (pinned at `7c3615a8`), where three changes reach this
backend. Still installs against released bmad-loop **0.9.0**: every change here is either
backward-compatible or internal.

- **`detach_client` reports effect, not dispatch (bmad-loop #227).** The seam widened from
  `None` to `bool`, and both client verbs now owe the caller an honest answer about whether
  a client actually moved. herdr detach is a keybinding (`ctrl+b q`) with no CLI verb, and
  protocol 17 exposes no client/attach object to measure across the call the way psmux
  counts its attached clients — so the answer is `False`, never a vacuous `True`. Core
  reads that as `ReturnOutcome.UNREACHABLE`: after a sweep decision it leaves the return
  option set for the parked trailer, prints nothing, and takes the sweep unattended
  (pending decisions stay reachable via `bmad-loop decisions`). **From your seat the
  visible change is that the `✓ decisions recorded` line no longer appears** — bmad-loop
  will not announce a hand-back it could not perform. Press `ctrl+b q` as before; the
  operator guide's detach section is rewritten around this.

  `switch_client` keeps its exit-code answer, which is honest for herdr — `tab focus` fails
  loudly on a tab that is not there. Its residue (focus moves, but a raw
  `herdr terminal attach` client does not follow it) is recorded in the ledger; the
  combination is unreachable through `bmad-loop attach`, which records a detach target for
  every attach from outside herdr.

  On released 0.9.0 the return value is discarded, so nothing changes there.

- **The `pipe_pane` tee streams output instead of piling up frames.** The poller appended a
  whole `pane read` snapshot on every content change; it now appends only the part of the
  frame that is new since the previous one, and the priming read merely SEEDS that baseline
  rather than logging the screen that was there when the tee attached. This is what tmux's
  `pipe-pane` hands core, and two new core consumers depend on the shape: the #194 scan
  reads the last 64 KiB of the log for a transport error (frames re-logged at ~1 Hz let a
  real error age out of that window in seconds), and the #261 proof-of-work gate reads the
  log's SIZE as evidence the CLI rendered anything at all (a logged pre-launch screen —
  prompt plus typed launch line — cleared that 256-byte floor for a session that did
  nothing). Alignment is by line overlap, with a second pass that tolerates a tail line
  still being drawn, so a spinner costs one line per tick rather than a frame. A frame with
  no alignment at all (alt-screen repaint, clear, or a scroll past the whole window inside
  one tick) is still appended whole — a re-log of known text, never a loss.

- **Transport failures pause the story on herdr too (bmad-loop #194), with one gap.**
  Verified live end to end: a CLI that prints `API Error … ECONNREFUSED` and idles out its
  session clock is classified off the herdr tee and pauses instead of burning a dev
  attempt. The gap is inherent to polling — an error line that appears and scrolls away
  inside one poll interval is never captured, and that story charges the attempt as it did
  before the feature. Fail-open, and now in the operator guide's differences table.

- Internal: a contract-drift guard test compares every abstract seam method's signature
  against ours, so the next upstream widening fails a test instead of degrading silently;
  return-file cleanup goes through `platform_util.retrying_unlink` (win32 sharing-violation
  retry).

## 0.3.0 — 2026-07-22

- **BREAKING: requires herdr ≥ 0.7.5 (server protocol 17).** The supported protocol is
  bumped 16 → 17; the backend now refuses any herdr server older than 0.7.5
  (`herdr server protocol 16 < required 17; upgrade herdr`) and still warns once above
  17. herdr 0.7.5 redesigned `agent start` to launch only known agent kinds — the old
  arbitrary-argv pane spawn it relied on is gone — so the launch surface no longer uses
  it on any platform.
- **Native-Windows launch rebuilt as a typed PowerShell `pane run`.** Both platform
  families now launch identically: `tab create` (carrying `--env`) spawns the tab's
  default shell, then the command is typed into that pane — POSIX `exec <argv>`, win32
  `& <argv>; exit $LASTEXITCODE` (the exec-mirror: the call operator runs argv, `exit`
  ends the shell with the child's status). This retires the old bootstrap-shell/agent
  two-pane launch and its win32-only quirks:
  - no rollback — a failed typed launch leaves one benign idle shell tab (the POSIX
    posture), and the two-pane phantom hazard is structurally gone;
  - the parked-window return-file path is embedded at compose time on win32 exactly as on
    POSIX (no runtime `HERDR_PANE_ID`/`Join-Path` derivation);
  - the sidecar return-option write precedes the typed recipe on both families (the old
    win32 ordering deviation is gone);
  - a best-effort `pane rename` labels the win32 launch's pane;
  - a bounded `pane wait-output` readiness wait (~10 s) precedes typing so keystrokes
    don't race PowerShell start-up.

  The Windows tab shell must stay PowerShell (herdr's `terminal.default_shell`, its
  default).
- **bmad-loop 0.9.0 multiplexer seam.** `window_pane_pids` now reports the pane's shell +
  foreground pids via `pane process-info` for core's kill escalation (degrading to `[]`
  on any failure, read as "unknown"); `current_return_target` is deliberately left at its
  seam default — the native pane id, unique server-wide because herdr runs one server for
  all sessions. Needs a bmad-loop new enough to carry this seam.
- **Selection unchanged, tracking core.** bmad-loop 0.9.0 makes psmux a bundled builtin
  and the win32 platform default; herdr is selected on win32 only when psmux is
  absent/unavailable (first-match) or explicitly forced (`mux set herdr` /
  `BMAD_LOOP_MUX_BACKEND=herdr`). tmux stays the POSIX default.
- `__version__` synced with `pyproject` (0.3.0).

## 0.2.0 — 2026-07-14

- **Native-Windows launch surface** (issue #1): windows launch via herdr's
  `agent start` (tab create keeps the window-name label; the agent process splits in
  and the bootstrap shell pane is closed strictly, so process-exit == pane-close ==
  tab-close on both platform families). Agent sidebar names are uniquified as
  `name@tab-id` (herdr agent names are server-global-unique).
- **pwsh parked-window dialect**: parked windows on Windows run a
  PowerShell-5.1-compatible recipe (`pwsh` preferred, `powershell.exe` fallback) riding
  `agent start` as a single argv element; the trailer derives its return-file path at
  runtime from the injected `HERDR_PANE_ID`.
- herdr output decodes as UTF-8 (with replacement) on win32; locale default elsewhere.
- Integration/E2E suites now run on Windows: the fake CLIs are portable Python scripts
  and the platform gates are gone (only a herdr binary is required).
- No selection change: psmux remains core's declared win32 default; herdr activates on
  win32 by first-match while no higher-priority backend is installed.

## 0.1.0 — 2026-07-14

- Initial release, extracted from bmad-loop core (developed there in PRs #136/#137).
- `HerdrMultiplexer`: the full `TerminalMultiplexer` contract over herdr 0.7.3
  (protocol 16) — workspace/tab mapping, typed `exec` launches, parked windows with
  per-window return files, polling `pipe_pane` tee, lock-guarded JSON sidecar for
  session/window options, TUI launch/attach surface.
- Self-registers via bmad-loop's `bmad_loop.mux_backends` entry-point group:
  install next to bmad-loop and `bmad-loop mux set herdr`.
- POSIX launches only (Linux/macOS/WSL); native-Windows launch surface tracked as a
  follow-up issue.
