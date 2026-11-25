"""Compatibility launcher; maintain covscreen.mcp, not this entry point."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("COVSCREEN_PROJECT_ROOT", str(SOURCE))).resolve()
sys.path.insert(0, str(SOURCE / "src"))

from covscreen.mcp import server
from covscreen.mcp.registry import TOOLS


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return server.dispatch(ROOT, name, arguments)


def dispatch_approved(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return server.dispatch_approved(ROOT, name, arguments)


def main() -> int:
    return server.main(ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
