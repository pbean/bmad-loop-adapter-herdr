"""herdr terminal-multiplexer backend for bmad-loop.

Importing :mod:`bmad_loop_adapter_herdr.backend` registers the backend with
bmad-loop's multiplexer registry; with the package installed next to bmad-loop,
the ``bmad_loop.mux_backends`` entry point does that import automatically.
"""

from .backend import HerdrError, HerdrMultiplexer

__version__ = "0.4.0"

__all__ = ["HerdrError", "HerdrMultiplexer", "__version__"]
