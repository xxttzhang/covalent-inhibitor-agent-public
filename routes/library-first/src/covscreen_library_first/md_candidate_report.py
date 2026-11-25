"""Versioned structural-triage policy and report for three-replica covalent MD."""
from __future__ import annotations

import json
from pathlib import Path

from .md_system_conversion import sha
from .md_system_minimization import _regular


POLICY = {
    "id": "library-first-covalent-md-structural-triage-v1",
    "scope": "target-agnostic structural triage for expert review",
    "minimum_frames_per_replica": 1000,
    "covalent_distance_range_A": [1.4, 2.4],
    "replica_criteria": {
        "protein_ca_rmsd_p95_max_A": 5.0,
        "pocket_ca_rmsd_p95_max_A": 3.0,
        "ligand_heavy_rmsd_p95_max_A": 4.0,
        "ligand_com_displacement_p95_max_A": 4.0,
        "contact_retention_median_min": 0.30,
    },
    "consensus": {
        "supportive_replicas": 3,
        "mixed_replicas": 2,
        "unfavorable_replicas_max": 1,
    },
}


def _evaluate_replica(replica):
    metrics = replica["metrics"]
    criteria = {
        "protein_ca_rmsd_p95": metrics["protein_ca_rmsd_A"]["p95"] <= 5.0,
        "pocket_ca_rmsd_p95": metrics["pocket_ca_rmsd_A"]["p95"] <= 3.0,
        "ligand_heavy_rmsd_p95": metrics["ligand_heavy_rmsd_A"]["p95"] <= 4.0,
        "ligand_com_displacement_p95": metrics["ligand_com_displacement_A"]["p95"] <= 4.0,
        "contact_retention_median": metrics["contact_retention"]["median"] >= 0.30,
    }
    technical = (replica["frames"] >= POLICY["minimum_frames_per_replica"]
        and metrics["covalent_distance_A"]["min"] >= 1.4
        and metrics["covalent_distance_A"]["max"] <= 2.4)
    return {"replica": replica["replica"], "frames": replica["frames"],
        "technical_gate_passed": technical, "criteria": criteria,
        "structurally_supportive": technical and all(criteria.values())}


def build_candidate_md_report(structural_analysis, structural_analysis_sha256, output):
    path = _regular(structural_analysis, structural_analysis_sha256, "Structural analysis")
    analysis = json.loads(path.read_text())
    if (analysis.get("schema") != "openmm-three-replica-structural-analysis-v1"
            or analysis.get("status") != "THREE_REPLICA_STRUCTURAL_ANALYSIS_COMPLETE_REPORT_PENDING"
            or analysis.get("replicas") != [1, 2, 3]
            or analysis.get("descriptive_only") is not True
            or len(analysis.get("replica_summaries", [])) != 3):
        raise ValueError("Structural analysis has not passed report admission")
    evaluations = [_evaluate_replica(item) for item in analysis["replica_summaries"]]
    technical = all(item["technical_gate_passed"] for item in evaluations)
    supportive = sum(item["structurally_supportive"] for item in evaluations)
    if not technical:
        classification = "TECHNICAL_REJECTION"
        recommendation = "REPAIR_OR_RERUN_BEFORE_SCIENTIFIC_REVIEW"
    elif supportive == 3:
        classification = "STRUCTURALLY_SUPPORTIVE_FOR_EXPERT_REVIEW"
        recommendation = "ADVANCE_TO_CROSS_EVIDENCE_EXPERT_REVIEW"
    elif supportive == 2:
        classification = "MIXED_STRUCTURAL_EVIDENCE"
        recommendation = "HOLD_FOR_EXPERT_REVIEW_OR_ADDITIONAL_SAMPLING"
    else:
        classification = "STRUCTURALLY_UNFAVORABLE_OR_INCONSISTENT"
        recommendation = "DO_NOT_ADVANCE_WITHOUT_EXPERT_REVIEW_OR_RERUN"
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Candidate MD report output must be new")
    output.mkdir(parents=True)
    result = {"schema": "library-first-covalent-md-candidate-assessment-v1",
        "status": "CANDIDATE_MD_STRUCTURAL_TRIAGE_COMPLETE",
        "system_id": analysis["system_id"],
        "parent": {"structural_analysis_sha256": structural_analysis_sha256},
        "policy": POLICY, "replica_evaluations": evaluations,
        "supportive_replicas": supportive, "classification": classification,
        "recommendation": recommendation,
        "structural_triage_decision_made": True,
        "expert_review_required": True,
        "experimental_activity_claimed": False, "affinity_claimed": False,
        "kinetics_claimed": False, "clinical_claimed": False,
        "limitations": [
            "Operational thresholds are versioned triage criteria, not universal biophysical laws.",
            "Covalent attachment constrains ligand displacement and does not prove favorable noncovalent recognition.",
            "The report must be combined with docking, chemistry, selectivity and experimental evidence.",
        ]}
    report = [f"# Library-first covalent MD report: {analysis['system_id']}", "",
        f"- Classification: `{classification}`", f"- Recommendation: `{recommendation}`",
        f"- Structurally supportive replicas: {supportive}/3", "",
        "## Replica assessment", ""]
    for item in evaluations:
        failed = [name for name, passed in item["criteria"].items() if not passed]
        report.append(f"- Replica {item['replica']}: technical={'PASS' if item['technical_gate_passed'] else 'FAIL'}; "
            f"structural={'SUPPORTIVE' if item['structurally_supportive'] else 'NOT SUPPORTIVE'}; "
            f"failed criteria={', '.join(failed) if failed else 'none'}.")
    report += ["", "## Interpretation limits", "",
        "This is a versioned structural-triage result for expert review. It does not establish affinity, "
        "kinetics, biochemical activity, selectivity, efficacy or clinical value.", ""]
    report_path = output / "candidate-md-report.md"
    report_path.write_text("\n".join(report))
    result["artifacts"] = {"candidate-md-report.md": sha(report_path)}
    (output / "candidate-md-assessment.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
