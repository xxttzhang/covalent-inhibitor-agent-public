from __future__ import annotations

import argparse
import json
from pathlib import Path

from covscreen.carsidock_results import audit_carsidock_outputs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-prefix", type=Path, required=True)
    ap.add_argument("--input-sdf", type=Path, required=True)
    args = ap.parse_args()
    audit = audit_carsidock_outputs(args.output_prefix, args.input_sdf)
    output = args.output_prefix.with_suffix(".audit.json")
    output.write_text(json.dumps(audit, indent=2) + "\n")
    print(json.dumps(audit, indent=2))
    return 0 if audit["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
