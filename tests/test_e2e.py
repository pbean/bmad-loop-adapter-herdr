"""Stories E2E over the herdr backend, gated on a live herdr install.

ONE happy-path scenario lifted from ``test_stories_e2e.py``'s fake-claude +
custom-profile-TOML recipe, driven through the REAL ``bmad-loop run`` binary with
the herdr backend forced by env (``BMAD_LOOP_MUX_BACKEND=herdr``) and a private
per-test herdr server — a throwaway socket + config/state root (see
``_isolate_herdr``), with a guaranteed server-stop finalizer. The scaffold, fake CLI,
profile TOML, and
assertions are reused verbatim from ``test_stories_e2e`` so this proves the exact
same full stack — arg parsing, prompt render, hook-signal completion, the stories
read-back, git commit, sprint advance — resolves over herdr as it does over tmux.

Deliberately just the two-story happy path: one full-stack pass proves the
CLI-through-herdr wiring end to end; the exhaustive stories/sprint/sweep matrix
stays on tmux in ``test_stories_e2e.py``. The module is skipped when herdr is not
installed. It runs on win32 too — the vendored recipe's fake CLI is Python,
landed via ``write_portable_cli`` — driving the ``pane run`` typed-launch surface
through the REAL ``bmad-loop run`` stack.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

# The deterministic fake-claude recipe (scaffold + assertions) is vendored from
# bmad-loop core's test_stories_e2e.py into _stories_recipe.py. Prepend import
# mode makes these cross-module imports work (no tests/__init__.py).
from _stories_recipe import CLI, _commit_count, _entry, _scaffold, _status
from test_integration import _isolate_herdr, _teardown_server

HAVE_HERDR = shutil.which("herdr") is not None
pytestmark = pytest.mark.skipif(not HAVE_HERDR, reason="stories E2E needs herdr")


@pytest.fixture
def herdr_env(tmp_path, monkeypatch):
    """Force the herdr backend and isolate its private 0.7.5 server + config/state
    root + sidecar for the real ``bmad-loop`` subprocess (see
    :func:`_isolate_herdr`; ``HERDR_SESSION`` is gone in 0.7.5). The child inherits
    these vars via ``_run``'s ``os.environ.copy()``, so a fresh process picks herdr
    with no cache to clear. Per-test private root => xdist-safe; the finalizer tears
    the private server down and removes the root even on failure."""
    base = _isolate_herdr(monkeypatch, tmp_path)
    try:
        yield base
    finally:
        _teardown_server(base)


def _run(root, *args, timeout=150) -> subprocess.CompletedProcess:
    # Inherits os.environ (the monkeypatched herdr vars) — same shape as
    # test_stories_e2e._run, kept local so the herdr env flows to the child.
    return subprocess.run(
        [*CLI, args[0], "--project", str(root), *args[1:]],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(root),
        env=os.environ.copy(),
    )


def test_herdr_e2e_two_story_happy_path(tmp_path, herdr_env):
    """The full CLI stack (test_stories_e2e's happy path) resolving over herdr: two
    stories dispatched to done through real herdr workspaces/tabs/panes, one
    squashed commit per story."""
    root = tmp_path / "sbx"
    _scaffold(root, [_entry("1"), _entry("2")])
    base = _commit_count(root)

    proc = _run(root, "run")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _status(root, "1") == "done"
    assert _status(root, "2") == "done"
    # one squashed story commit per story above the sandbox baseline
    assert _commit_count(root) == base + 2
