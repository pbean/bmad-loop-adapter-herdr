"""Registry/selection proof for the herdr adapter, against bmad-loop's registry.

Adapted from core's ``tests/test_backend_registry.py`` herdr section: the same
six selection facts, re-anchored on how an OUT-OF-TREE backend registers — a
module-level ``register_multiplexer`` call that runs at import time (directly,
or via the ``bmad_loop.mux_backends`` entry point) — instead of core's builtin
loader. Plus the encoder-inheritance pin from core's ``test_multiplexer.py``, an
entry-point metadata smoke test, and a bmad-loop-0.9.0 fact: psmux is now a
bundled builtin and the win32 platform default, so herdr wins on win32 only when
psmux is absent/unavailable (or when herdr is explicitly forced).

The ``fresh_registry`` fixture mirrors core's: snapshot/clear/restore the
module-global registry, then replay this package's registration into the empty
registry (import-time registration already ran once per process, so the clear
would otherwise erase us). Builtins still load lazily inside ``_select`` —
AFTER our entry — which is exactly the ordering an early entry-point import
produces; every assertion here is name-keyed or first-match-stable under it.
"""

from __future__ import annotations

import importlib.metadata
import shutil
import sys

import pytest

from bmad_loop.adapters import multiplexer as m
from bmad_loop.adapters.psmux_backend import PsmuxMultiplexer
from bmad_loop.adapters.tmux_backend import TmuxMultiplexer

from bmad_loop_adapter_herdr.backend import HerdrMultiplexer


@pytest.fixture
def fresh_registry(monkeypatch):
    """Isolate the global registry + lru_cache + configured choice (snapshot,
    clear, restore — core's recipe), then re-register herdr the way the module
    does at import. Externals-loader state (core ≥ the discovery seam) is saved
    and restored too when present, so a real entry-point scan cannot double-load
    or leak errors across tests."""
    monkeypatch.delenv("BMAD_LOOP_MUX_BACKEND", raising=False)
    saved_backends = list(m._BACKENDS)
    saved_loaded = m._BUILTINS_LOADED
    saved_configured = m._CONFIGURED
    has_externals = hasattr(m, "_EXTERNALS_LOADED")
    if has_externals:
        saved_ext_loaded = m._EXTERNALS_LOADED
        saved_ext_errors = dict(m._EXTERNAL_ERRORS)
    m._BACKENDS.clear()
    m._BUILTINS_LOADED = False
    m._CONFIGURED = None
    if has_externals:
        # our replay below IS the external load; a live scan re-importing this
        # (already-imported) module would register nothing new anyway
        m._EXTERNALS_LOADED = True
        m._EXTERNAL_ERRORS.clear()
    m.register_multiplexer("herdr", lambda platform: True, HerdrMultiplexer)
    m.get_multiplexer.cache_clear()
    yield m
    m._BACKENDS[:] = saved_backends
    m._BUILTINS_LOADED = saved_loaded
    m._CONFIGURED = saved_configured
    if has_externals:
        m._EXTERNALS_LOADED = saved_ext_loaded
        m._EXTERNAL_ERRORS.clear()
        m._EXTERNAL_ERRORS.update(saved_ext_errors)
    m.get_multiplexer.cache_clear()


def _which_only(*available: str):
    """`shutil.which` stub: only the named binaries resolve, everything else is
    absent. Patches the shared stdlib module, so both tmux_base and this backend
    (which each `import shutil`) see it."""
    names = set(available)
    return lambda name, *a, **k: (f"/usr/bin/{name}" if name in names else None)


def test_herdr_registered_and_tmux_wins_on_posix(fresh_registry, monkeypatch):
    """Both backends available on POSIX: herdr is registered but tmux is the
    platform default, so tmux still wins (herdr is opt-in on POSIX)."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", _which_only("tmux", "herdr"))
    backend, name, reason = fresh_registry._select()
    assert isinstance(backend, TmuxMultiplexer)
    assert (name, reason) == ("tmux", "platform-default")


def test_herdr_first_match_when_tmux_unavailable_on_posix(fresh_registry, monkeypatch):
    """A POSIX host with herdr but no tmux binary selects herdr as the first
    available platform match — the default (tmux) is unavailable and falls through."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", _which_only("herdr"))  # tmux absent
    backend, name, reason = fresh_registry._select()
    assert isinstance(backend, HerdrMultiplexer)
    assert (name, reason) == ("herdr", "first-match")


def test_env_override_selects_herdr(fresh_registry, monkeypatch):
    """BMAD_LOOP_MUX_BACKEND=herdr forces herdr by name, bypassing the platform
    predicate and availability (an explicit choice is trusted, as for any backend)."""
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "herdr")
    fresh_registry.get_multiplexer.cache_clear()
    assert isinstance(fresh_registry.get_multiplexer(), HerdrMultiplexer)


def test_herdr_selected_on_win32_as_first_platform_match(fresh_registry, monkeypatch):
    """On native Windows tmux does not match (its `matches` is `p != 'win32'`) and
    psmux — a bundled builtin in bmad-loop 0.9.0 and the declared win32 default —
    has no resolvable binary here, so it probes unavailable; with this adapter
    installed the cross-platform herdr is then the first AVAILABLE platform match:
    win32 auto-selects herdr, `_PLATFORM_DEFAULTS` still naming psmux."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", _which_only("herdr"))  # herdr present, no tmux/psmux
    backend, name, reason = fresh_registry._select()
    assert isinstance(backend, HerdrMultiplexer)
    assert (name, reason) == ("herdr", "first-match")
    # sanity: _PLATFORM_DEFAULTS still names psmux for win32, not herdr
    assert fresh_registry._PLATFORM_DEFAULTS.get("win32") == "psmux"


def test_win32_psmux_is_platform_default_when_available(fresh_registry, monkeypatch):
    """bmad-loop 0.9.0 makes psmux a bundled builtin AND the win32 platform
    default: with its binary (plus pwsh) resolvable and the version gate passing,
    psmux wins as `platform-default`, AHEAD of herdr's first-match — even with
    this adapter installed. herdr stays reachable by an explicit force."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", _which_only("psmux", "pwsh", "herdr"))
    # psmux.available() version-gates on a real `psmux -V`; stub it to a supported
    # release (> 3.3.6) so the probe passes without spawning a subprocess.
    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 3.5")
    backend, name, reason = fresh_registry._select()
    assert isinstance(backend, PsmuxMultiplexer)
    assert (name, reason) == ("psmux", "platform-default")

    # herdr is still selectable on win32 by explicit force, bypassing the default.
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "herdr")
    fresh_registry.get_multiplexer.cache_clear()
    assert isinstance(fresh_registry.get_multiplexer(), HerdrMultiplexer)


def test_win32_falls_back_to_herdr_even_when_unavailable(fresh_registry, monkeypatch):
    """With no binary installed on win32, the historical fallback returns the first
    platform match regardless of availability — herdr, once this adapter is
    installed (without it, core bottom-falls-back to tmux)."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", _which_only())  # nothing available
    backend, name, reason = fresh_registry._select()
    assert isinstance(backend, HerdrMultiplexer)
    assert (name, reason) == ("herdr", "fallback")


def test_detect_multiplexers_lists_herdr_row(fresh_registry, monkeypatch):
    """detect_multiplexers() enumerates herdr with its availability reflecting the
    host probe. version() is stubbed so no row spawns a real `herdr --version` /
    `tmux -V`."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", _which_only("tmux", "herdr"))
    monkeypatch.setattr(HerdrMultiplexer, "version", lambda self: "herdr 0.7.5")
    monkeypatch.setattr(TmuxMultiplexer, "version", lambda self: "tmux 3.5")
    rows = {r.name: r for r in fresh_registry.detect_multiplexers()}
    assert {"tmux", "herdr"} <= set(rows)
    assert rows["herdr"].matches_platform is True
    assert rows["herdr"].available is True
    assert rows["herdr"].version == "herdr 0.7.5"
    # tmux is the POSIX platform default, so it stays selected — herdr is listed but not chosen
    assert rows["tmux"].selected is True
    assert rows["herdr"].selected is False and rows["herdr"].reason == ""


def test_herdr_inherits_the_default_encoder():
    # herdr resolves targets lazily at use time (_parse_target), so it must NOT
    # override target() to eagerly emit native ids — a token formatted ahead of
    # use (e.g. attach_plan's return_window) would go stale.
    assert "target" not in HerdrMultiplexer.__dict__
    assert HerdrMultiplexer().target("s", "w") == "=s:w"


def test_entry_point_is_declared():
    """The installed distribution advertises the backend module under bmad-loop's
    ``bmad_loop.mux_backends`` entry-point group, so a core with the discovery
    seam imports (and thereby registers) it with no config step."""
    eps = importlib.metadata.entry_points(group="bmad_loop.mux_backends")
    ours = [ep for ep in eps if ep.name == "herdr"]
    assert ours, "bmad_loop.mux_backends entry point 'herdr' not found — package not installed?"
    assert ours[0].value == "bmad_loop_adapter_herdr.backend"


def test_version_matches_distribution_metadata():
    """``__version__`` and the installed distribution's version (from ``pyproject``)
    must agree — a drift-guard so bumping one without the other fails loudly (both
    were bumped 0.2.0 -> 0.3.0 for the herdr-0.7.5 release). CI fresh-syncs, so the
    installed metadata is always current there."""
    from bmad_loop_adapter_herdr import __version__

    assert importlib.metadata.version("bmad-loop-adapter-herdr") == __version__
