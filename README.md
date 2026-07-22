# bmad-loop-adapter-herdr

A [herdr](https://herdr.dev) terminal-multiplexer backend for
[bmad-loop](https://github.com/bmad-code-org/bmad-loop), the deterministic BMAD
orchestrator. It implements bmad-loop's `TerminalMultiplexer` seam on top of herdr's
workspace/tab model, so agent sessions, the TUI's launch/attach keys, log tees, stall
detection, and completion probes all run over herdr instead of tmux — including on hosts
where tmux isn't an option.

Characterized against **herdr 0.7.5** (server protocol **17**). Launches are native on
both platform families and typed into a fresh tab's default shell via `pane run`: a
typed `exec <argv>` on POSIX (Linux, macOS, WSL) and, on Windows, a typed
`& <argv>; exit $LASTEXITCODE` in PowerShell, where parked windows run a
PowerShell-5.1-compatible recipe (`pwsh` preferred when installed, `powershell.exe`
otherwise). The Windows tab shell must stay PowerShell (herdr's `terminal.default_shell`,
its default). Note that herdr's own Windows build ships on a preview channel.

## Install

bmad-loop discovers external backends through the `bmad_loop.mux_backends` entry-point
group, so installing this package **next to bmad-loop** is the whole setup. Neither
package is on PyPI — both install straight from GitHub.

If you use bmad-loop as a `uv` tool (the standard setup-flow install):

```bash
uv tool install "bmad-loop @ git+https://github.com/bmad-code-org/bmad-loop.git" \
  --with "bmad-loop-adapter-herdr @ git+https://github.com/pbean/bmad-loop-adapter-herdr"
```

(Already have the tool installed? Re-run the same command — `uv tool install` upgrades
in place — or add `--force`.)

In a plain virtualenv, install both into the same environment:

```bash
pip install "bmad-loop @ git+https://github.com/bmad-code-org/bmad-loop.git" \
            "bmad-loop-adapter-herdr @ git+https://github.com/pbean/bmad-loop-adapter-herdr"
```

Then:

```bash
bmad-loop mux            # the herdr row appears (platform / availability / version)
bmad-loop mux set herdr  # persist the choice for this machine
```

tmux remains bmad-loop's default; herdr is opt-in via `mux set herdr` or
`BMAD_LOOP_MUX_BACKEND=herdr`. See the
[operator guide](docs/adapter-multiplexer-herdr.md) for what changes from your seat on
herdr (one manual detach chord, `ctrl+b q`; polled logs; a JSON state sidecar).

> **Version note:** entry-point discovery needs a bmad-loop new enough to include the
> `bmad_loop.mux_backends` scan. On older cores the package installs fine but the
> backend stays invisible — upgrade bmad-loop.

## Development

```bash
uv sync                        # creates .venv with bmad-loop (from git) + dev deps
uv run pytest -q               # unit tests run everywhere;
                               # integration/E2E auto-skip without a herdr binary
uv run black --check src tests
```

The integration and E2E tests drive a **real, private herdr server** per test —
isolated onto a throwaway socket plus config/state root (`HERDR_SOCKET_PATH` +
`XDG_CONFIG_HOME`/`XDG_STATE_HOME`), so they never touch your own herdr server or its
session state — and are skipped automatically when `herdr` is not on PATH. They run on
Windows too — the fake CLIs are Python scripts typed through the `pane run` launch
surface.

## Provenance

The backend was developed in-tree in bmad-loop core
([#136](https://github.com/bmad-code-org/bmad-loop/pull/136),
[#137](https://github.com/bmad-code-org/bmad-loop/pull/137)) and extracted here,
pre-release, as the reference out-of-tree adapter. Code is MIT, © 2025 BMad Code, LLC —
see [LICENSE](LICENSE). To write your own backend, start with core's
[adapter authoring guide](https://github.com/bmad-code-org/bmad-loop/blob/main/docs/adapter-authoring-guide.md)
and [porting guide](https://github.com/bmad-code-org/bmad-loop/blob/main/docs/porting-to-a-new-os.md).
