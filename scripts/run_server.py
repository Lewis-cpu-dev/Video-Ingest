"""Launch the backend from its checkout, regardless of the MCP client's working directory."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

if __name__ == "__main__":
    from server.main import main
    main()
