"""Execute the chain's next CarsiDock stage using retained poses, not new docking."""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import json
from pathlib import Path

from covscreen.execution import JobManager
from covscreen.ligand_chain import (
    load_ligand_chain_config, inspect_ligand_chain, compile_next_ligand_request,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--retained-dir", type=Path, required=True)
    parser.add_argument("--human-approved", action="store_true")
    parser.add_argument("--approval-note", required=True)
    args = parser.parse_args()
    if not args.human_approved or not args.approval_note.strip():
        parser.error("Explicit approval is required")
    root = args.project_root.resolve()
    config_path = args.config.resolve()
    config_path.relative_to(root)
    retained = args.retained_dir.resolve().relative_to(root)
    config = load_ligand_chain_config(config_path)
    status = inspect_ligand_chain(root, config)
    if status.next_stage != "carsidock" or status.terminal_state != "ready":
        raise ValueError("CarsiDock is not the chain's sole ready next stage; no submission made")
    request = compile_next_ligand_request(root, config, status, execution_mode="execute",
                                         human_approved=True, approval_note=args.approval_note)
    request.parameters.update(reuse_grouped_output_dir=str(retained), device="cpu")
    request.request_id += "-retained-reparse"
    request.resource_budget = request.resource_budget.model_copy(update={
        "gpu_count": 0, "cpu_count": 1, "memory_gb": 4, "max_runtime_minutes": 15})
    result = JobManager(root).submit(request)
    print(json.dumps(result.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
