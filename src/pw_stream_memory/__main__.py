from __future__ import annotations

import os
import sys

from pw_stream_memory.app import main

if __name__ == "__main__":
    os.environ.setdefault("ESCDELAY", "25")
    raise SystemExit(main())
