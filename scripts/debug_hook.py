"""Prompt-hook launcher. Always exits 0, including when the memory command fails."""

from __future__ import annotations

import sys

from eng_graph import cli as eng

try:
    eng.main()
except SystemExit:
    pass
except Exception:
    pass
sys.exit(0)
