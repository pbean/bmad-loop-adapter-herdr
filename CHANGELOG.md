# Changelog

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
