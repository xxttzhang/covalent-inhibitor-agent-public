"""Export an explicitly scoped reuse report; never mark the full campaign complete."""
from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
from rdkit import Chem, rdBase
from rdkit.Chem import Crippen, Descriptors, FilterCatalog

from covscreen.pareto import select_pareto_candidates
from covscreen.covdock_gate import assess_covdock_candidates
from covscreen.upstream_handoff import WARHEADS


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(source: Path, output: Path, pose_evidence: Path | None = None) -> dict:
    table = source / "partial_evaluated_candidates.csv"
    summary_path = source / "partial_probe_summary.json"
    summary = json.loads(summary_path.read_text())
    frame = pd.read_csv(table)
    if summary.get("status") != "partial_probe_only":
        raise ValueError("Expected explicitly partial recovery evidence")
    if len(frame) != summary["input_count"] or frame.input_sdf_index.duplicated().any():
        raise ValueError("Source identity denominator mismatch")
    selected = frame.loc[frame.input_sdf_index.isin([59, 65])].copy()
    if set(selected.input_sdf_index) != {59, 65} or len(selected) != 2:
        raise ValueError("Both frozen recovery candidates are required")
    if not selected.independent_gate.eq("PASS").all():
        raise ValueError("Recovered candidates must pass independent evaluation")
    if not selected.coordinate_recheck_status.eq("SUCCESS").all():
        raise ValueError("Recovered candidates require successful coordinate reread")
    pose_sources = []
    if pose_evidence is not None:
        manifest_path = pose_evidence / "pose_consistency_manifest.json"
        pose_manifest = json.loads(manifest_path.read_text())
        pose_csv = pose_evidence / "af3_covdock_pose_consistency.csv"
        if (pose_manifest.get("status") != "completed"
                or pose_manifest.get("input_count") != 2
                or pose_manifest.get("success_count") != 2
                or digest(pose_csv) != pose_manifest["outputs"][pose_csv.name]):
            raise ValueError("Incomplete or changed pose evidence")
        poses = pd.read_csv(pose_csv)
        if (len(poses) != 2 or poses.input_sdf_index.duplicated().any()
                or set(poses.input_sdf_index) != {59, 65}
                or not poses.pose_consistency_status.eq("SUCCESS").all()
                or not poses.pose_topology_status.eq("VERIFIED_FIXED_NAME_GRAPH").all()):
            raise ValueError("Pose evidence identities or topology invalid")
        poses = poses.set_index("input_sdf_index")
        identity_fields = ("compound_id", "covdock_pose_file", "covdock_pose_index", "model_cif")
        metric_fields = ("af3_covdock_ligand_rmsd_angstrom", "protein_alignment_rmsd_angstrom",
                         "pose_consistency_status", "pose_topology_status")
        for index, row in selected.iterrows():
            pose = poses.loc[row.input_sdf_index]
            if any(pose[field] != row[field] for field in identity_fields):
                raise ValueError("Pose evidence source identity mismatch")
            for field in metric_fields:
                selected.loc[index, field] = pose[field]
        selected = assess_covdock_candidates(selected)
        pose_sources = [manifest_path, pose_csv]
    params = FilterCatalog.FilterCatalogParams()
    params.AddCatalog(FilterCatalog.FilterCatalogParams.FilterCatalogs.PAINS)
    catalog = FilterCatalog.FilterCatalog(params)
    # Recompute, never replace missing measurements with an assumed zero.
    for index, row in selected.iterrows():
        mol = Chem.MolFromSmiles(row.standardized_smiles)
        if mol is None:
            raise ValueError("Invalid recovered candidate SMILES")
        sites = set()
        for name, (smarts, _, _) in WARHEADS.items():
            for match in mol.GetSubstructMatches(Chem.MolFromSmarts(smarts)):
                sites.add((name, match[-2] if name == "chloroacetamide" else match[-1]))
        if not sites:
            raise ValueError("No supported warhead identified during risk annotation")
        selected.loc[index, "supported_warhead_count"] = len(sites)
        selected.loc[index, "high_reactivity_alert"] = len(sites) > 1
        selected.loc[index, "aggregation_risk"] = Crippen.MolLogP(mol) > 5 and Descriptors.MolWt(mol) > 500
        match = catalog.GetFirstMatch(mol)
        selected.loc[index, "pains_alert"] = match.GetDescription() if match else ""
    output.mkdir(parents=True, exist_ok=False)
    selected.to_csv(output / "reused_candidates.csv", index=False)
    selection = select_pareto_candidates(selected, output / "pareto", final_n=2,
                                         profile="ligand-first")
    payload = {
        "schema_version": "1.0", "mode": "historical_result_reuse",
        "scope": "two_recovered_candidates_only", "selected_input_indices": [59, 65],
        "source_partial_denominator": len(frame), "historical_af3_selected_count": 24,
        "historical_covdock_batch": "time_limit_interrupted",
        "remaining_22": "not_scheduled_under_revised_scope",
        "full_campaign_completed": False, "platform_acceptance_completed": False,
        "new_calculation_submitted": False,
        "source_evidence": [{"path": str(p.resolve().relative_to(_bootstrap.PROJECT)), "sha256": digest(p)}
                            for p in (table, summary_path, *pose_sources)],
        "upstream_hashes": {k: v for k, v in summary.items() if k.endswith("_sha256")},
        "pareto_status": selection["status"], "pareto_quality": selection["quality"],
        "objectives": selection["selection_policy"]["objectives"],
        "risk_annotation": {"rdkit_version": rdBase.rdkitVersion,
                            "high_reactivity_alert": "more_than_one_site_in_supported_four_warhead_catalog",
                            "aggregation_risk": "RDKit_MolLogP_gt_5_and_MolWt_gt_500",
                            "pains_alert": "RDKit_PAINS_first_match",
                            "interpretation": "structural_heuristics_not_measured_reactivity_or_safety"},
        "limitations": ["No new docking executed", "Not evidence of potency or method superiority",
                        ("Fixed-name RMSD verified; not symmetry-corrected" if pose_sources else
                         "Cross-method RMSD not completed for both candidates"),
                        "Source hashes preserve provenance; original remote structures are not bundled"],
    }
    (output / "reuse_manifest.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    lines = ["# Ligand-first 两候选结果复用报告", "",
             "本报告交付历史真实计算结果，不代表新任务执行或平台端到端验收完成。", "",
             "- 范围：input59 与 input65；原 AF3 Top24 的 CovDock 批处理因超时中断。",
             "- 两个候选通过此前坐标复读与独立化学/几何规则评价；其余22个按新目标不再安排补算。",
             f"- 本次 Pareto 状态：{selection['status']}；选出 {selection['quality']['selected_count']} 个。",
             "- 沿用既有 Ligand-first 非支配排序，不使用加权总分。", "",
             "目标及方向：" + json.dumps(payload["objectives"], ensure_ascii=False), "",
             "候选表、结构 SDF 和排序解释位于 pareto/；SDF 的二维结构不是共价结合构象。",
             ("两候选均完成键图验证和局部对齐；门控按既有阈值重算。构象一致不等于活性或方法优势。"
              if pose_sources else "两候选的跨方法局部对齐尚未全部完成；不能据此宣称高构象共识、活性或方法优势。"),
             "源文件与上游证据哈希记录在 reuse_manifest.json；未修改原始阶段状态。", ""]
    (output / "README.md").write_text("\n".join(lines))
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pose-evidence", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.source, args.output, args.pose_evidence), indent=2, ensure_ascii=False))
