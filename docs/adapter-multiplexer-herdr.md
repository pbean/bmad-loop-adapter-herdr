# herdr backend — operator guide

[herdr](https://herdr.dev) is a cross-platform, agent-aware terminal workspace manager —
a background server plus a CLI — whose agent-status sidebar is a natural fit for watching
bmad-loop runs. With this adapter installed the backend is **opt-in**: tmux stays
bmad-loop's default on POSIX, and herdr is selected only when you ask for it.

```bash
bmad-loop mux set herdr                    # persist for this machine
BMAD_LOOP_MUX_BACKEND=herdr bmad-loop run  # …or force a single invocation
```

`bmad-loop mux` lists the registered backends and shows which one is selected and why;
see [Terminal multiplexer backends](https://github.com/bmad-code-org/bmad-loop/blob/main/docs/multiplexer-backends.md)
in bmad-loop core for the full selection precedence.

A bmad-loop **session** becomes a herdr **workspace** (same name) and each **window** a
**tab**. From your seat, runs behave as on tmux: windows close when the agent's process
exits, parked `[bmad-loop exited <code> — press enter]` windows wait for Enter, and the
TUI's Log tab, stall detection, and completion probes all work. One difference is
functional and needs a keypress from you; the rest are cosmetic or invisible.

## Detach is manual: press `ctrl+b q`

herdr has no CLI command to detach an attached terminal — detach exists only as herdr's
own keybinding, by default `ctrl+b q` (`ctrl+b ctrl+b` sends a literal `ctrl+b`). So the
one moment where bmad-loop would detach your terminal **for** you becomes manual:

- **When it bites** — you attached from a plain terminal (or the TUI suspended itself to
  attach you) to answer a sweep decision. On tmux, answering prints
  `✓ decisions recorded — sweep continues in the background` and your shell or dashboard
  comes back by itself. On herdr your terminal **stays attached** to the sweep's window,
  and **nothing is printed** — the backend reports honestly that it could not hand your
  terminal back, and bmad-loop does not announce a return that did not happen.
- **What to do** — press `ctrl+b q`. Nothing is lost or waiting on you: the answer is
  already recorded and the sweep continues, unattended, in that window. The keypress only
  hands your terminal back. A suspended TUI resumes the moment the attach exits.
- **What the sweep does meanwhile** — it stops prompting for the rest of the run (it
  cannot tell whether anyone is still watching, and a `--repeat` cycle blocking forever on
  a question nobody can see is the worse way to be wrong). Later decisions are recorded
  rather than asked; read and answer them with `bmad-loop decisions`. The run journals
  `sweep-return-no-client` at that point, which is the line to look for if you are
  wondering why the prompts stopped.
- **What still returns automatically** — everything that rides a window _closing_: when a
  parked window's command finishes and you press Enter, the pane closes and the blocking
  attach exits on its own, exactly as on tmux. Attaching from **inside** herdr is a tab
  switch and behaves like tmux's `switch-client`.

The same applies wherever bmad-loop's TUI guide says `ctrl-b d`: on herdr the detach
chord is `ctrl+b q`, and the post-answer hand-back after a sweep decision cannot be
automatic — press it yourself. Detaching _before_ you finish is also fine; the sweep goes
unattended either way.

## Differences you may notice

| Where                          | On tmux                                  | On herdr                                                                                                              | What to do                                                                               |
| ------------------------------ | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Session logs (TUI Log tab)     | output streams continuously (a tmux tee) | the pane is polled about once a second and only what is _new_ is appended, so a static screen doesn't grow the log and the log starts where the tee attached (not with the scrollback already on screen) | nothing — stall detection and completion markers read the same log and work unchanged    |
| Transport-failure pauses       | an `API Error … Connection refused` in the pane log pauses the story instead of burning a dev attempt | same, off the same log — but an error line that appears and scrolls away inside one poll interval is never captured, so that story burns the attempt as it did before the feature | nothing; if a story deferred after repeated timeouts, check its pane log for a transport error the poll missed |
| Backend state                  | lives in the tmux server                 | lives in a lock-guarded JSON sidecar, `~/.bmad-loop/herdr-state.json` (override: `BMAD_LOOP_HERDR_STATE`)             | leave it alone while runs are live; entries for gone workspaces are pruned automatically |
| Switching back after an attach | falls back to your most recent client    | herdr has no "last client" — if the pane you came from is gone, focus stays put                                       | switch tabs yourself                                                                     |
| Detached window size           | honors the requested geometry headless   | advisory — a detached pane takes the size of whichever client attaches                                                | nothing; the first attach may briefly reflow                                             |
| Server lifecycle               | any tmux command starts the server       | bmad-loop starts the herdr server lazily, and only for operations that create or change something                     | nothing — `bmad-loop mux`, `validate`, and listings never resurrect a stopped server     |
| Pane labels (Windows)          | n/a                                      | Windows-launched panes get a best-effort label (herdr `pane rename`) matching the window name; a rename failure is purely cosmetic | nothing — informational; window names in bmad-loop itself are unchanged                  |

## Current limits

- **Windows rides herdr's preview build.** Launches are native on Windows — the command
  is typed into a fresh tab's default PowerShell pane via `pane run` (a
  `& <argv>; exit $LASTEXITCODE`, the mirror of the POSIX `exec`), and parked windows run
  a PowerShell-5.1-compatible recipe (`pwsh` when installed, `powershell.exe` otherwise),
  with the same banner and Enter-to-close behavior as POSIX. Two things follow from the
  typed-into-PowerShell launch: the Windows tab shell **must stay PowerShell** — herdr's
  `terminal.default_shell` is PowerShell by default, and repointing it at cmd.exe or
  another shell breaks the launch; and because the launch waits for the shell prompt
  before typing, a heavily customized PowerShell profile adds start-up latency (bounded —
  the readiness wait gives up after ~10 s and types anyway). herdr itself ships Windows
  binaries on a preview channel, so expect that platform to mature with herdr. The
  `ctrl+b q` detach guidance above applies unchanged.
- **Don't hand-create `bmad-loop-*` workspaces.** herdr allows duplicate workspace labels
  (tmux session names are unique); bmad-loop refuses to create a duplicate itself and
  always resolves the first match, but a hand-made duplicate can shadow a run's real
  workspace.
- **Version pin.** The backend is characterized against herdr **0.7.5** (server protocol
  **17**): it refuses to run below that protocol and warns once above it.
