"""pw-stream-memory: ncurses editor for PipeWire / WirePlumber stream restore."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pw-stream-memory")
except PackageNotFoundError:
    __version__ = "0.1.0"


def main() -> int:
    from pw_stream_memory.app import main as _main

    return _main()
