"""Read the bounded EGFR recovery demonstration without launching computation."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


def read_minimal_reuse(root: Path, *, target: str, site: str, parent: str) -> dict:
    if (target.upper(), site.upper(), parent.lower()) != ("EGFR", "C797", "erlotinib"):
        return {"status": "no_matching_reuse", "new_calculation_submitted": False}
    root = root.resolve()
    report = root / "reports/ligand_minimal_reuse_20260924_v3"
    upgraded = root / "reports/ligand_minimal_reuse_20260924_v4"
    if upgraded.exists():
        report = upgraded
    manifest_path = report / "reuse_manifest.json"
    if not manifest_path.is_file():
        return {"status": "reuse_not_installed", "new_calculation_submitted": False}

    def checked(path: str, expected: str) -> Path:
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("Reuse evidence must stay within the project")
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != expected:
            raise ValueError("Reuse evidence hash mismatch: " + str(resolved))
        return resolved

    if not manifest_path.resolve().is_relative_to(root):
        raise ValueError("Reuse manifest escapes project")
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("mode") != "historical_result_reuse"
            or manifest.get("selected_input_indices") != [59, 65]
            or manifest.get("platform_acceptance_completed") is not False
            or manifest.get("new_calculation_submitted") is not False):
        raise ValueError("Unexpected reuse scope")
    for evidence in manifest["source_evidence"]:
        checked(evidence["path"], evidence["sha256"])
    selection_path = report / "pareto/stage6_pareto_manifest.json"
    if not selection_path.resolve().is_relative_to(root):
        raise ValueError("Selection manifest escapes project")
    selection = json.loads(selection_path.read_text())
    if selection["status"] != "completed" or not all(selection["quality_gates"].values()):
        raise ValueError("Reuse Pareto quality gate failed")
    outputs = {name: checked(record["path"], record["sha256"])
               for name, record in selection["outputs"].items()}
    with outputs["stage6_final_candidates.csv"].open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 2 or {int(row["input_sdf_index"]) for row in rows} != {59, 65}:
        raise ValueError("Final candidate identities changed")
    fields = ("compound_id", "input_sdf_index", "pareto_front", "af3_mpae",
              "local_strain_energy", "independent_gate", "covdock_gate",
              "af3_covdock_ligand_rmsd_angstrom", "physical_validity",
              "cross_method_consensus", "pose_topology_status")
    return {"status": "historical_reuse_available", "target": "EGFR", "site": "C797",
            "parent": "erlotinib", "candidate_count": 2,
            "candidates": [{key: row.get(key) for key in fields} for row in rows],
            "source_hashes_verified": True, "new_calculation_submitted": False,
            "platform_acceptance_completed": False,
            "report_markdown": outputs["stage6_selection_report.md"].read_text(),
            "artifact_path_scope": "Paths are relative to the scientific backend project, not the chat workspace. Use the verified inline report and candidates; do not glob the chat workspace for these files.",
            "artifact_sha256": {name: record["sha256"] for name, record in selection["outputs"].items()},
            "artifact_notes": {
                "stage6_final_candidates.sdf": "2D chemical structures; not 3D binding poses",
                "paths": "Return exact artifact paths without ellipses; do not invent download URLs",
                "gate_A": "Computed physical validity and cross-method pose consensus; not experimental validation",
            },
            "limitations": manifest["limitations"],
            "artifacts": {name: str(path.relative_to(root)) for name, path in outputs.items()}}
