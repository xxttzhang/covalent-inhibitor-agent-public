from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from covscreen.execution import execute_persisted_job


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one persisted, approved covalent job.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = execute_persisted_job(args.project_root, args.run_id)
    return 0 if result.state == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
