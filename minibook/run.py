#!/usr/bin/env python3
"""Run Minibook server."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minibook.src.main import run

if __name__ == "__main__":
    run()
