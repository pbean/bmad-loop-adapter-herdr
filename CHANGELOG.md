# Changelog

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
