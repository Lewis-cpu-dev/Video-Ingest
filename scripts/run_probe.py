"""Launch Gate 0 from any working directory, including a tunnel-client stdio profile."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

if __name__ == "__main__":
    from server.probe import main
    main()
