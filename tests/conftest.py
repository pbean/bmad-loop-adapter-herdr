"""Test-suite root marker.

Its presence puts this directory on ``sys.path`` under pytest's default
prepend import mode, which the cross-module test imports rely on
(``test_poller`` reuses ``test_backend``'s FakeHerdr; ``test_e2e`` reuses
``test_integration``'s teardown and the vendored ``_stories_recipe``).

No shared fixtures live here on purpose: every herdr fixture (the FakeHerdr
transport, the isolated per-test herdr server, the sidecar redirect into
``tmp_path``) is defined next to its tests, exactly as it was in bmad-loop
core before the extraction.
"""
