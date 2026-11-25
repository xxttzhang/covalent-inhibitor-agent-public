from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
from pathlib import Path

from covscreen.ligand_acceptance import audit_ligand_p0_closure


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the exact Ligand-first P0 closure evidence.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--af3-run", required=True)
    parser.add_argument("--covdock-run", required=True)
    parser.add_argument("--evaluation-run", required=True)
    parser.add_argument("--pareto-run", required=True)
    parser.add_argument("--expected-count", type=int, default=3)
    parser.add_argument("--delivery-table", type=Path)
    parser.add_argument("--platform-receipt", type=Path)
    parser.add_argument("--browser-verification", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit_ligand_p0_closure(
        args.project_root,
        {"af3": args.af3_run, "covdock": args.covdock_run,
         "evaluation": args.evaluation_run, "pareto": args.pareto_run},
        expected_count=args.expected_count,
        delivery_table=args.delivery_table,
        platform_receipt=args.platform_receipt,
        browser_verification=args.browser_verification,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "failed_gates": result["failed_gates"]}, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 4


if __name__ == "__main__":
    raise SystemExit(main())
