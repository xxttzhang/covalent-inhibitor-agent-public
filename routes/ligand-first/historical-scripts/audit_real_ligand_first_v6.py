#!/usr/bin/env python3
from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = _bootstrap.PROJECT
sys.path.insert(0, str(ROOT / "src"))

from covscreen.tool_api import evaluate_ligand_first_candidate_tool  # noqa: E402


PROJECT_ID = "egfr-c797"
BASE = Path("projects/egfr-c797/ligand_first")
CANDIDATE_ID = "egfr-f9f62be250f14d9a"
CARSIDOCK_RUN_ID = "20260817T135020-carsidock-cov-be42799293"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check(name: str, passed: bool, evidence: Any) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "evidence": evidence}


def mcp_tool_names(root: Path) -> set[str]:
    message = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    ) + "\n"
    result = subprocess.run(
        [sys.executable, "-u", str(root / "scripts/openscience_mcp_server.py")],
        cwd=root,
        input=message,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    response = json.loads(result.stdout.strip())
    return {item["name"] for item in response["result"]["tools"]}


def audit(root: Path) -> dict[str, Any]:
    campaign = read_json(root / BASE / "campaign.json")
    inputs = read_json(root / BASE / "derived/input_audit.json")
    generated = read_json(root / BASE / "generated/candidates.json")
    generation_failures = read_json(root / BASE / "generated/failures.json")
    geometry = read_json(root / BASE / "geometry/geometry_evaluation.json")
    run_manifest = read_json(
        root
        / BASE
        / "geometry/carsidock_20260817T135020/carsidock.run_manifest.json"
    )
    passport_path = root / BASE / "passports" / f"{CANDIDATE_ID}.passport.json"
    evaluation_path = (
        root / BASE / "passports" / f"{CANDIDATE_ID}.independent_evaluation.json"
    )
    passport = read_json(passport_path)
    saved_independent = read_json(evaluation_path)
    candidate = generated["candidates"][0]
    chemistry = candidate["quality_gate"]
    selected = geometry["selected_pose"]

    tool_payload = {
        "project_id": PROJECT_ID,
        "campaign_path": "ligand_first/campaign.json",
        "candidate_json_path": "ligand_first/generated/candidates.json",
        "candidate_sdf_path": "ligand_first/generated/candidates.sdf",
        "geometry_json_path": "ligand_first/geometry/geometry_evaluation.json",
        "raw_poses_path": (
            "ligand_first/geometry/carsidock_20260817T135020/carsidock.sdf"
        ),
        "no_dummy_poses_path": (
            "ligand_first/geometry/carsidock_20260817T135020/carsidock_nodumat.sdf"
        ),
        "expected_carsidock_run_id": CARSIDOCK_RUN_ID,
    }
    recomputation = evaluate_ligand_first_candidate_tool(root, tool_payload)
    recomputed = recomputation["independent_evaluation"]

    checks: list[dict[str, Any]] = []
    checks.append(
        check(
            "campaign.real_target_parent_and_site",
            campaign["target"]["symbol"] == "EGFR"
            and campaign["target"]["uniprot_id"] == "P00533"
            and campaign["target"]["nucleophile"] == {
                "residue_name": "CYS",
                "uniprot_residue_number": 797,
                "atom_name": "SG",
            }
            and campaign["parent_pose"]["pdb_id"] == "1M17"
            and campaign["parent_pose"]["ligand_component_id"] == "AQ4"
            and campaign["parent_pose"]["ligand_name"] == "erlotinib"
            and campaign["parent_pose"]["experimental_method"] == "X-RAY DIFFRACTION"
            and campaign["parent_pose"]["target_author_residue_number"] == 773
            and campaign["parent_pose"]["target_uniprot_residue_number"] == 797,
            {
                "target": campaign["target"],
                "parent_pose": campaign["parent_pose"],
                "covalent_reference_ids": [
                    item["pdb_id"] for item in campaign["covalent_references"]
                ],
            },
        )
    )
    checks.append(
        check(
            "inputs.assets_protection_and_exit_vectors",
            inputs["status"] == "PASS"
            and inputs["errors"] == []
            and all(inputs["asset_checks"].values())
            and len(inputs["scaffold_core_atom_indices"]) >= 1
            and len(inputs["protected_pharmacophore_atom_indices"]) >= 1
            and all(item["retained"] for item in inputs["protected_interactions"])
            and any(item["advance"] for item in inputs["exit_vectors"])
            and any(not item["advance"] for item in inputs["exit_vectors"])
            and len(campaign["protection"]["protected_substituent_bonds"]) >= 1,
            {
                "asset_checks": inputs["asset_checks"],
                "scaffold_core_atom_indices": inputs["scaffold_core_atom_indices"],
                "protected_pharmacophore_atom_indices": inputs[
                    "protected_pharmacophore_atom_indices"
                ],
                "protected_interactions": inputs["protected_interactions"],
                "exit_vectors": inputs["exit_vectors"],
            },
        )
    )
    checks.append(
        check(
            "generation.modern_rdkit_rule_and_lineage",
            generated["generated_count"] == 1
            and generated["chemistry_pass_count"] == 1
            and candidate["candidate_id"] == CANDIDATE_ID
            and candidate["provenance"] == "deterministic-rdkit-covalentizer"
            and candidate["rule_id"] == "rdkit-replace-aryl-ether-with-acrylamide-v1"
            and candidate["warhead_name"] == "acrylamide"
            and bool(candidate["parent_atom_index_to_product_index"])
            and bool(candidate["warhead_atom_map_to_product_index"]),
            {
                "candidate_id": candidate["candidate_id"],
                "canonical_smiles": candidate["canonical_smiles"],
                "provenance": candidate["provenance"],
                "rule_id": candidate["rule_id"],
                "exit_vector_id": candidate["exit_vector_id"],
                "reaction_atom_map_number": chemistry["reaction_atom_map_number"],
            },
        )
    )
    chemistry_pass = (
        chemistry["status"] == "PASS"
        and chemistry["sanitization_ok"]
        and chemistry["valence_ok"]
        and chemistry["radical_electron_count"] == 0
        and chemistry["formal_charge_allowed"]
        and chemistry["single_component"]
        and chemistry["protected_core_retained"]
        and chemistry["protected_pharmacophore_retained"]
        and chemistry["protected_substituent_bonds_retained"]
        and chemistry["exactly_one_allowed_warhead"]
        and chemistry["reaction_atom_mapping_valid"]
        and chemistry["reaction_atom_map_number"] == 906
        and chemistry["failure_reasons"] == []
    )
    checks.append(check("chemistry.all_required_quality_gates", chemistry_pass, chemistry))
    checks.append(
        check(
            "execution.real_carsidock_pose_ensemble",
            run_manifest["status"] == "SUCCESS"
            and run_manifest["return_code"] == 0
            and run_manifest["engine"] == "carsidock-cov"
            and run_manifest["output_audit"]["docking_success"]
            and run_manifest["output_audit"]["pose_count"] == 9
            and geometry["carsidock_run_id"] == CARSIDOCK_RUN_ID
            and geometry["pose_count"] == 9,
            {
                "engine": run_manifest["engine"],
                "status": run_manifest["status"],
                "runtime_seconds": run_manifest["runtime_seconds"],
                "output_audit": run_manifest["output_audit"],
                "job_run_id": geometry["carsidock_run_id"],
            },
        )
    )
    checks.append(
        check(
            "geometry.scaffold_interaction_reaction_strain_clash_gates",
            geometry["status"] == "PASS"
            and geometry["passing_pose_count"] == 6
            and geometry["ensemble_pass_fraction"] >= 0.5
            and all(selected["gate_flags"].values())
            and selected["scaffold_rmsd_angstrom"] <= 2.0
            and selected["interaction_retention_fraction"] >= 0.7
            and 1.65 <= selected["covalent_cs_bond_distance_angstrom"] <= 2.15
            and 80.0 <= selected["attack_angle_s_cbeta_calpha_degrees"] <= 140.0
            and selected["strain_excess_over_parent_kcal_mol"] <= 15.0
            and selected["clash_fraction"] <= 0.2
            and any(
                pose["status"] == "FAIL"
                and "scaffold_rmsd" in pose["failure_reasons"]
                and "interaction_retention" in pose["failure_reasons"]
                and "protein_clash" in pose["failure_reasons"]
                for pose in geometry["poses"]
            ),
            {
                "pose_count": geometry["pose_count"],
                "passing_pose_count": geometry["passing_pose_count"],
                "ensemble_pass_fraction": geometry["ensemble_pass_fraction"],
                "selected_pose": selected,
                "rejected_pose_indices": [
                    pose["pose_index"]
                    for pose in geometry["poses"]
                    if pose["status"] == "FAIL"
                ],
            },
        )
    )

    artifact_checks: dict[str, bool] = {}
    for artifact in passport["artifacts"]:
        artifact_path = root / artifact["path"]
        artifact_checks[artifact["path"]] = (
            artifact_path.is_file()
            and artifact_path.stat().st_size == artifact["size_bytes"]
            and sha256(artifact_path) == artifact["sha256"]
        )
    checks.append(
        check(
            "passport.unified_identity_lineage_and_artifact_integrity",
            passport["schema_version"] == "2.0"
            and passport["route"] == "ligand-first"
            and passport["candidate_id"] == CANDIDATE_ID
            and passport["target"]["canonical_site"] == "CYS797"
            and passport["parent_pose"]["pdb_id"] == "1M17"
            and passport["molecular_identity"]["reaction_atom_map_number"] == 906
            and passport["design_lineage"]["generator"]
            == "deterministic-rdkit-covalentizer"
            and bool(artifact_checks)
            and all(artifact_checks.values()),
            {
                "passport_id": passport["passport_id"],
                "candidate_id": passport["candidate_id"],
                "artifact_checks": artifact_checks,
            },
        )
    )
    checks.append(
        check(
            "evaluation.independent_source_artifact_recomputation",
            recomputation["status"] == "PASS"
            and recomputed["evaluator_id"] == "ligand-first-independent-evaluator-v1"
            and recomputed["status"] == "PASS"
            and recomputed["decision"] == "ADVANCE"
            and all(recomputed["verified_checks"].values())
            and recomputed["recomputed_chemistry_gate"]["status"] == "PASS"
            and recomputed["recomputed_geometry"]["status"] == "PASS"
            and recomputed == saved_independent
            and passport["independent_evaluation"] == saved_independent,
            {
                "evaluator_id": recomputed["evaluator_id"],
                "status": recomputed["status"],
                "decision": recomputed["decision"],
                "verified_checks": recomputed["verified_checks"],
                "next_stage_handoffs": recomputed["next_stage_handoffs"],
            },
        )
    )
    mcp_names = mcp_tool_names(root)
    evaluator_agent = (
        root / ".openscience/agent/ligand-first-independent-evaluator.md"
    ).read_text()
    planner_agent = (
        root / ".openscience/agent/ligand-first-attachment-planner.md"
    ).read_text()
    checks.append(
        check(
            "openscience.independent_agent_and_read_only_mcp_tool",
            "covalent_evaluate_ligand_first_candidate" in mcp_names
            and "mode: subagent" in evaluator_agent
            and "edit: deny" in evaluator_agent
            and "bash: deny" in evaluator_agent
            and "covalent_evaluate_ligand_first_candidate" in evaluator_agent
            and "covalent_evaluate_ligand_first_candidate" in planner_agent,
            {
                "mcp_tool_registered": "covalent_evaluate_ligand_first_candidate"
                in mcp_names,
                "evaluator_agent": (
                    ".openscience/agent/ligand-first-independent-evaluator.md"
                ),
                "planner_handoff": (
                    ".openscience/agent/ligand-first-attachment-planner.md"
                ),
            },
        )
    )
    failure_types = {
        item.get("failure_type") for item in passport.get("failure_ledger", [])
    }
    checks.append(
        check(
            "provenance.failed_hypotheses_and_runtime_attempt_retained",
            generation_failures["failure_count"] == 1
            and "PROTECTED_NON_ADVANCING_EXIT" in failure_types
            and "CXXABI_RUNTIME_ORDER" in failure_types,
            {
                "generation_failures": generation_failures["failures"],
                "passport_failure_types": sorted(item for item in failure_types if item),
            },
        )
    )

    passed = sum(item["status"] == "PASS" for item in checks)
    return {
        "phase": "real-ligand-first-closed-loop-v6",
        "campaign_id": campaign["campaign_id"],
        "candidate_id": CANDIDATE_ID,
        "carsidock_run_id": CARSIDOCK_RUN_ID,
        "checks_total": len(checks),
        "checks_passed": passed,
        "checks_failed": len(checks) - passed,
        "status": "PASS" if passed == len(checks) else "FAIL",
        "checks": checks,
        "scientific_boundary": (
            "ADVANCE means this computational Ligand-first hypothesis passed declared chemistry "
            "and pose-ensemble gates. It does not establish synthesis, biochemical potency, "
            "kinact/KI, selectivity, safety, cellular activity, or clinical value."
        ),
    }


def markdown(report: dict[str, Any]) -> str:
    geometry_check = next(
        item
        for item in report["checks"]
        if item["name"]
        == "geometry.scaffold_interaction_reaction_strain_clash_gates"
    )
    selected = geometry_check["evidence"]["selected_pose"]
    lines = [
        "# 真实 Ligand-first 闭环 V6 验收",
        "",
        f"- 总状态：**{report['status']}**",
        f"- 检查：{report['checks_passed']}/{report['checks_total']} PASS",
        f"- 候选：`{report['candidate_id']}`",
        f"- CarsiDock 任务：`{report['carsidock_run_id']}`",
        "",
        "| 要求/检查 | 状态 |",
        "|---|---|",
    ]
    lines.extend(
        f"| `{item['name']}` | {item['status']} |" for item in report["checks"]
    )
    lines.extend(
        [
            "",
            "## 冻结结果",
            "",
            f"- Pose ensemble：{geometry_check['evidence']['passing_pose_count']}/"
            f"{geometry_check['evidence']['pose_count']} 通过。",
            f"- Scaffold RMSD：{selected['scaffold_rmsd_angstrom']:.3f} Å。",
            f"- Met793 相互作用保持：{selected['interaction_retention_fraction']:.1%}。",
            f"- C–S 距离：{selected['covalent_cs_bond_distance_angstrom']:.3f} Å。",
            f"- S–Cβ–Cα 角：{selected['attack_angle_s_cbeta_calpha_degrees']:.2f}°。",
            f"- 相对母体 strain：{selected['strain_excess_over_parent_kcal_mol']:.3f} kcal/mol。",
            f"- Clash fraction：{selected['clash_fraction']:.1%}。",
            "- 独立评价：`PASS / ADVANCE`。",
            "",
            "## 科学边界",
            "",
            report["scientific_boundary"],
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT / "reports/REAL_LIGAND_FIRST_V6_AUDIT.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT / "reports/REAL_LIGAND_FIRST_V6_ACCEPTANCE.md",
    )
    args = parser.parse_args()
    report = audit(args.root.resolve())
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    args.markdown_output.write_text(markdown(report))
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks_passed": report["checks_passed"],
                "checks_total": report["checks_total"],
                "candidate_id": report["candidate_id"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
