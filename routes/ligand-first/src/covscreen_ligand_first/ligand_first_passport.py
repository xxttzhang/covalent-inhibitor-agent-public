from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from rdkit import Chem

from .ligand_first_geometry import (
    LigandFirstGeometryPolicy,
    LigandFirstGeometryResult,
    evaluate_carsidock_geometry,
)
from .ligand_first_inputs import (
    LigandFirstCampaignSpec,
    verify_campaign_assets,
)
from .rdkit_covalentizer import ChemistryQualityGate, evaluate_chemistry_quality


class ArtifactEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    role: str


class IndependentEvaluationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    require_input_asset_checks: bool = True
    require_recomputed_chemistry_pass: bool = True
    require_recomputed_geometry_pass: bool = True
    minimum_ensemble_pass_fraction: float = Field(default=0.50, ge=0, le=1)
    metric_consistency_tolerance: float = Field(default=1e-6, gt=0)


class IndependentEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    evaluator_id: Literal["ligand-first-independent-evaluator-v1"] = (
        "ligand-first-independent-evaluator-v1"
    )
    status: Literal["PASS", "FAIL"]
    decision: Literal["ADVANCE", "HOLD"]
    verified_checks: dict[str, bool]
    failure_reasons: list[str]
    recomputed_chemistry_gate: ChemistryQualityGate
    recomputed_geometry: LigandFirstGeometryResult
    policy: IndependentEvaluationPolicy
    next_stage_handoffs: dict[str, Literal["READY", "NOT_READY", "NOT_RUN"]]
    limitations: list[str]


class UnifiedCandidatePassport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "2.0"
    passport_id: str
    candidate_id: str
    campaign_id: str
    route: Literal["ligand-first"] = "ligand-first"
    target: dict[str, Any]
    parent_pose: dict[str, Any]
    molecular_identity: dict[str, Any]
    design_lineage: dict[str, Any]
    chemistry_quality: dict[str, Any]
    structural_evaluation: dict[str, Any]
    independent_evaluation: IndependentEvaluationResult
    failure_ledger: list[dict[str, Any]]
    artifacts: list[ArtifactEvidence]
    evidence_boundaries: list[str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_evidence(project_root: Path, path: Path, role: str) -> ArtifactEvidence:
    resolved_root = project_root.resolve()
    resolved = path.resolve()
    relative = resolved.relative_to(resolved_root)
    return ArtifactEvidence(
        path=str(relative),
        sha256=_sha256(resolved),
        size_bytes=resolved.stat().st_size,
        role=role,
    )


def _canonical_smiles_without_maps(mol: Chem.Mol) -> str:
    copy = Chem.Mol(mol)
    for atom in copy.GetAtoms():
        atom.SetAtomMapNum(0)
    return Chem.MolToSmiles(copy, canonical=True)


def _geometry_consistent(
    saved: LigandFirstGeometryResult,
    recomputed: LigandFirstGeometryResult,
    tolerance: float,
) -> bool:
    if (
        saved.candidate_id != recomputed.candidate_id
        or saved.carsidock_run_id != recomputed.carsidock_run_id
        or saved.status != recomputed.status
        or saved.pose_count != recomputed.pose_count
        or saved.passing_pose_count != recomputed.passing_pose_count
        or saved.selected_pose_index != recomputed.selected_pose_index
    ):
        return False
    if abs(saved.ensemble_pass_fraction - recomputed.ensemble_pass_fraction) > tolerance:
        return False
    if saved.selected_pose is None or recomputed.selected_pose is None:
        return saved.selected_pose is None and recomputed.selected_pose is None
    metrics = (
        "scaffold_rmsd_angstrom",
        "interaction_retention_fraction",
        "covalent_cs_bond_distance_angstrom",
        "attack_angle_s_cbeta_calpha_degrees",
        "dummy_to_target_sg_distance_angstrom",
        "clash_fraction",
    )
    return all(
        abs(getattr(saved.selected_pose, name) - getattr(recomputed.selected_pose, name))
        <= tolerance
        for name in metrics
    )


def independently_evaluate_candidate(
    *,
    campaign: LigandFirstCampaignSpec,
    campaign_dir: Path,
    parent_pose: Chem.Mol,
    candidate_record: dict[str, Any],
    candidate_mol: Chem.Mol,
    saved_geometry: LigandFirstGeometryResult,
    expected_carsidock_run_id: str,
    raw_poses: list[Chem.Mol],
    no_dummy_poses: list[Chem.Mol],
    receptor_pdb: Path,
    policy: IndependentEvaluationPolicy | None = None,
) -> IndependentEvaluationResult:
    policy = policy or IndependentEvaluationPolicy()
    recomputed_chemistry = evaluate_chemistry_quality(candidate_mol, parent_pose, campaign)
    recomputed_geometry = evaluate_carsidock_geometry(
        campaign=campaign,
        parent_pose=parent_pose,
        candidate_id=str(candidate_record["candidate_id"]),
        carsidock_run_id=expected_carsidock_run_id,
        raw_poses=raw_poses,
        no_dummy_poses=no_dummy_poses,
        receptor_pdb=receptor_pdb,
        policy=LigandFirstGeometryPolicy.model_validate(saved_geometry.policy.model_dump()),
    )
    asset_checks = verify_campaign_assets(campaign, campaign_dir)
    sdf_candidate_id = candidate_mol.GetProp("candidate_id") if candidate_mol.HasProp("candidate_id") else None
    sdf_smiles = _canonical_smiles_without_maps(candidate_mol)
    original_chemistry = candidate_record.get("quality_gate", {})
    checks = {
        "source_assets_verified": bool(asset_checks) and all(asset_checks.values()),
        "candidate_id_consistent": (
            sdf_candidate_id == candidate_record.get("candidate_id") == saved_geometry.candidate_id
        ),
        "canonical_smiles_consistent": sdf_smiles == candidate_record.get("canonical_smiles"),
        "original_chemistry_gate_passed": original_chemistry.get("status") == "PASS",
        "chemistry_recomputation_passed": recomputed_chemistry.status == "PASS",
        "geometry_recomputation_passed": recomputed_geometry.status == "PASS",
        "carsidock_run_id_consistent": (
            saved_geometry.carsidock_run_id == expected_carsidock_run_id
        ),
        "geometry_artifact_consistent": _geometry_consistent(
            saved_geometry, recomputed_geometry, policy.metric_consistency_tolerance
        ),
        "ensemble_support_sufficient": (
            recomputed_geometry.ensemble_pass_fraction >= policy.minimum_ensemble_pass_fraction
        ),
        "geometry_recomputation_error_free": not recomputed_geometry.errors,
    }
    required = {
        "source_assets_verified": policy.require_input_asset_checks,
        "chemistry_recomputation_passed": policy.require_recomputed_chemistry_pass,
        "geometry_recomputation_passed": policy.require_recomputed_geometry_pass,
    }
    failed = [name for name, passed in checks.items() if not passed]
    for name, enabled in required.items():
        if enabled and not checks[name] and name not in failed:
            failed.append(name)
    passed = not failed
    handoff = "READY" if passed else "NOT_READY"
    return IndependentEvaluationResult(
        status="PASS" if passed else "FAIL",
        decision="ADVANCE" if passed else "HOLD",
        verified_checks=checks,
        failure_reasons=failed,
        recomputed_chemistry_gate=recomputed_chemistry,
        recomputed_geometry=recomputed_geometry,
        policy=policy,
        next_stage_handoffs={
            "reactivity_qm_agent": handoff,
            "admet_synthesis_agent": handoff,
            "md_evaluation_agent": handoff,
            "experimental_validation": "NOT_RUN",
        },
        limitations=[
            "The evaluator is execution-independent from generation and docking, but uses the same audited RDKit geometry library.",
            "CarsiDock constrains its dummy sulfur to the target SG coordinate; dummy-target overlap is an integrity check, not independent evidence of reaction probability.",
            "MMFF94s strain is interpreted relative to the experimental parent pose to control structure/force-field systematic bias.",
            "No QM reactivity, ADMET, synthesis, MD, selectivity, kinetic, cellular, or experimental evidence has been generated.",
        ],
    )


def build_unified_candidate_passport(
    *,
    campaign: LigandFirstCampaignSpec,
    candidate_record: dict[str, Any],
    geometry: LigandFirstGeometryResult,
    independent: IndependentEvaluationResult,
    artifacts: list[ArtifactEvidence],
    failure_ledger: list[dict[str, Any]],
) -> UnifiedCandidatePassport:
    identity = str(candidate_record["candidate_id"])
    passport_hash = hashlib.sha256(
        f"{campaign.campaign_id}|{identity}|{independent.status}|{geometry.carsidock_run_id}".encode()
    ).hexdigest()[:20]
    return UnifiedCandidatePassport(
        passport_id=f"passport-{passport_hash}",
        candidate_id=identity,
        campaign_id=campaign.campaign_id,
        target={
            "symbol": campaign.target.symbol,
            "uniprot_id": campaign.target.uniprot_id,
            "canonical_site": (
                f"{campaign.target.nucleophile.residue_name}"
                f"{campaign.target.nucleophile.uniprot_residue_number}"
            ),
            "structure_author_site": (
                f"{campaign.parent_pose.protein_chain_id}:"
                f"{campaign.target.nucleophile.residue_name}:"
                f"{campaign.parent_pose.target_author_residue_number}"
            ),
        },
        parent_pose={
            "pdb_id": campaign.parent_pose.pdb_id,
            "ligand_component_id": campaign.parent_pose.ligand_component_id,
            "ligand_name": campaign.parent_pose.ligand_name,
            "pose_source": "experimental_xray",
            "resolution_angstrom": campaign.parent_pose.resolution_angstrom,
            "pdb_doi": campaign.parent_pose.pdb_doi,
        },
        molecular_identity={
            "canonical_smiles": candidate_record["canonical_smiles"],
            "warhead": candidate_record["warhead_name"],
            "reaction_class": candidate_record["reaction_class"],
            "covalent_mode": candidate_record["covalent_mode"],
            "reaction_atom_map_number": candidate_record["quality_gate"][
                "reaction_atom_map_number"
            ],
        },
        design_lineage={
            "generator": candidate_record["provenance"],
            "rule_id": candidate_record["rule_id"],
            "transformation_name": candidate_record["transformation_name"],
            "exit_vector_id": candidate_record["exit_vector_id"],
            "removed_parent_atom_indices": candidate_record["removed_parent_atom_indices"],
            "parent_atom_index_to_product_index": candidate_record[
                "parent_atom_index_to_product_index"
            ],
            "warhead_atom_map_to_product_index": candidate_record[
                "warhead_atom_map_to_product_index"
            ],
        },
        chemistry_quality=candidate_record["quality_gate"],
        structural_evaluation=geometry.model_dump(mode="json"),
        independent_evaluation=independent,
        failure_ledger=failure_ledger,
        artifacts=artifacts,
        evidence_boundaries=[
            "Candidate generation is a deterministic medicinal-chemistry hypothesis, not a synthesis or activity claim.",
            "Geometry PASS means at least one pose met the declared structural gates; it does not prove covalent reaction kinetics.",
            "The candidate is computational and has not been synthesized or experimentally tested.",
        ],
    )
