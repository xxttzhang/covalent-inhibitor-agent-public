"""Bounded background retrieval; survives an MCP client disconnect."""
import sys
import os
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("COVSCREEN_PROJECT_ROOT", str(SOURCE))).resolve()
sys.path.insert(0, str(SOURCE / "src"))
from covscreen.target_hypothesis import run_collection, folder, save

if __name__ == "__main__":
    identifier = sys.argv[1]
    try:
        run_collection(ROOT, identifier)
    except Exception as error:
        status = folder(ROOT, identifier) / "status.json"
        if not status.exists():
            save(status, {"state": "FAILED", "reason": type(error).__name__, "detail": str(error)})
        raise
