from __future__ import annotations

import argparse
import json
from pathlib import Path

from covscreen.candidate_delivery import (
    build_candidate_results_manifest,
    write_candidate_results_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a verified three-route candidate-results-v2 manifest.")
    parser.add_argument("--candidate-table", type=Path, required=True)
    parser.add_argument("--route", choices=["library-first", "ligand-first", "de-novo"], required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--selection-report", type=Path)
    parser.add_argument("--traceability-report", type=Path)
    parser.add_argument("--traceability-json", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    documents = {
        role: path for role, path in {
            "selection_report": args.selection_report,
            "traceability_report": args.traceability_report,
            "traceability_json": args.traceability_json,
        }.items() if path is not None
    }
    payload = build_candidate_results_manifest(
        args.candidate_table,
        route=args.route,
        task_id=args.task_id,
        expected_count=args.expected_count,
        documents=documents,
    )
    write_candidate_results_manifest(payload, args.output)
    print(json.dumps({
        "status": "PASS",
        "schema": payload["schema"],
        "route": payload["route"],
        "candidate_count": len(payload["candidates"]),
        "asset_count": len(payload["assets"]),
        "document_count": len(payload["documents"]),
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
