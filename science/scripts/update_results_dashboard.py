from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from covscreen.dashboard import refresh_dashboard


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the OpenScience covalent workflow dashboard snapshot.")
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    dashboard = refresh_dashboard(args.project_root, args.run_id)
    print(json.dumps(dashboard.get("library_first", {}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
