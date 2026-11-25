"""Register completed grouped CovDock poses in the existing Ligand-first chain.

Default is a read-only plan. Execution requires explicit approval. The retained
runner independently checks molecular identities, source hashes and parameters;
this entry never substitutes a successful manifest for actual re-parsing.
"""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import json
from pathlib import Path

from covscreen.execution import JobManager
from covscreen.jobs import sha256_file
from covscreen.ligand_chain import (
    load_ligand_chain_config, inspect_ligand_chain, compile_next_ligand_request,
)


def validate_retained(root: Path, retained: Path, candidate: Path) -> dict:
    retained.relative_to(root)
    manifest = json.loads((retained / "covdock_lo_registered_manifest.json").read_text())
    gates = manifest.get("gate_quality_gates", {})
    if manifest.get("status") != "completed" or not gates or not all(v is True for v in gates.values()):
        raise ValueError("Retained CovDock has not passed all grouped quality gates")
    if manifest.get("input_candidate_table_sha256") != sha256_file(candidate):
        raise ValueError("Retained CovDock input hash differs from registered AF3 candidates")
    outputs = manifest.get("outputs", {})
    if "stage5_gated_candidates.parquet" not in outputs:
        raise ValueError("Retained CovDock is missing its candidate artifact")
    for name, metadata in outputs.items():
        path = (retained / name).resolve()
        path.relative_to(retained)
        if not path.is_file() or sha256_file(path) != metadata.get("sha256"):
            raise ValueError(f"Retained output missing or changed: {name}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--retained-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--human-approved", action="store_true")
    parser.add_argument("--approval-note", default="")
    args = parser.parse_args()
    if args.execute and (not args.human_approved or not args.approval_note.strip()):
        parser.error("Execution requires explicit approval and note")
    root, config_path, retained = args.project_root.resolve(), args.config.resolve(), args.retained_dir.resolve()
    config_path.relative_to(root)
    config = load_ligand_chain_config(config_path)
    status = inspect_ligand_chain(root, config)
    if status.next_stage != "covdock_lo" or status.terminal_state != "ready":
        raise ValueError("CovDock is not the chain's ready next stage; nothing submitted")
    request = compile_next_ligand_request(root, config, status,
        execution_mode="execute" if args.execute else "plan",
        human_approved=args.human_approved, approval_note=args.approval_note)
    validate_retained(root, retained, root / request.parameters["candidate_table"])
    request.parameters.update(reuse_artifact_dir=str(retained.relative_to(root)), njobs=1)
    request.request_id += "-retained-reparse"
    request.resource_budget = request.resource_budget.model_copy(update={
        "gpu_count": 0, "cpu_count": 2, "memory_gb": 8, "max_runtime_minutes": 60})
    # Planning does not create a PLANNED registry entry that would block execution.
    result = JobManager(root).submit(request) if args.execute else request
    print(json.dumps(result.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
