from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
import os
from pathlib import Path

from covscreen.execution import JobManager
from covscreen.ligand_chain import (
    compile_next_ligand_request,
    inspect_ligand_chain,
    ligand_chain_provenance,
    load_ligand_chain_config,
)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and compile the next registered Ligand-first stage.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execution-mode", choices=["plan", "execute"], default="plan")
    parser.add_argument("--human-approved", action="store_true")
    parser.add_argument("--approval-note")
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    config_path = args.config.resolve()
    config = load_ligand_chain_config(config_path)
    status = inspect_ligand_chain(root, config)
    request = compile_next_ligand_request(
        root, config, status, execution_mode=args.execution_mode,
        human_approved=args.human_approved, approval_note=args.approval_note,
    )
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "status": status.model_dump(mode="json"),
        "next_request": request.model_dump(mode="json") if request else None,
        "provenance": ligand_chain_provenance(root, config_path),
    }
    _atomic_json(args.output_dir / "ligand_first_chain_status.json", payload)
    if args.submit:
        if request is None:
            raise SystemExit("No next request is ready to submit")
        result = JobManager(root).submit(request)
        payload["submitted_result"] = result.model_dump(mode="json")
        _atomic_json(args.output_dir / "ligand_first_chain_status.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
