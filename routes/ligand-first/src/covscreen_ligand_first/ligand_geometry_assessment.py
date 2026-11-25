"""Versioned interpretation of measured ligand-first constraints, not activity."""
from __future__ import annotations

import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdkit import rdBase

from .ligand_first_geometry import (
    LigandFirstGeometryPolicy, PoseGeometryEvaluation, LigandFirstGeometryResult,
)

VERSION = "ligand-geometry-qc-v2-provisional"
Status = Literal["PASS", "FAIL", "UNKNOWN", "NOT_EVALUATED"]
PHYSICAL = ("covalent_cs_distance", "attack_angle", "actual_target_cs_distance",
            "actual_target_product_angle", "protein_clash", "severe_protein_clash_absent")
DESIGN = ("scaffold_rmsd", "interaction_retention", "ligand_strain")
TECHNICAL = ("dummy_target_alignment",)


class ProvisionalGeometryPolicy(LigandFirstGeometryPolicy):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    version: Literal["ligand-geometry-qc-v2-provisional"] = VERSION
    calibration_status: Literal["provisional"] = "provisional"
    ensemble_eligibility: Literal["one_complete_jointly_passing_pose_no_evaluation_errors"] = "one_complete_jointly_passing_pose_no_evaluation_errors"


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["ligand-independent-axes-v1"] = "ligand-independent-axes-v1"
    physical_validity: Status
    design_constraints: Status
    technical_qc: Status
    evidence_complete: bool
    review_eligible: bool
    missing_reasons: list[str]
    failure_reasons: list[str]
    calibration_status: Literal["provisional"] = "provisional"


class StrainContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit: Literal["kcal/mol"] = "kcal/mol"
    estimator: Literal["local_relaxation_convergence_checked_v1"] = "local_relaxation_convergence_checked_v1"
    reference_state: Literal["same_molecule_single_start_local_minimum"] = "same_molecule_single_start_local_minimum"
    candidate_representation: Literal["no_dummy_product_fragment_with_added_hydrogens"] = "no_dummy_product_fragment_with_added_hydrogens"
    parent_representation: Literal["parent_ligand_with_added_hydrogens"] = "parent_ligand_with_added_hydrogens"
    candidate_forcefield: Literal["MMFF94s", "UFF"] | None
    parent_forcefield: Literal["MMFF94s", "UFF"] | None
    candidate_converged: bool
    parent_converged: bool
    comparison_status: Literal["AVAILABLE", "UNAVAILABLE", "METHOD_MISMATCH"]
    rdkit_version: str = Field(min_length=1)
    max_iterations: Literal[1000] = 1000
    limitation: Literal["isolated_fragment_local_relaxation_not_full_adduct_or_binding_energy"] = "isolated_fragment_local_relaxation_not_full_adduct_or_binding_energy"


def finite(value):
    return value is not None and math.isfinite(value)


def axis(flags, names):
    values = [flags[name] for name in names]
    if False in values:
        return "FAIL"
    return "UNKNOWN" if None in values else "PASS"


def assess(flags):
    if set(flags) != set(PHYSICAL + DESIGN + TECHNICAL):
        raise ValueError("Unexpected or missing pose constraint keys")
    missing = [name for name, value in flags.items() if value is None]
    failed = [name for name, value in flags.items() if value is False]
    return Assessment(physical_validity=axis(flags, PHYSICAL),
        design_constraints=axis(flags, DESIGN), technical_qc=axis(flags, TECHNICAL),
        evidence_complete=not missing, review_eligible=not missing and not failed,
        missing_reasons=missing, failure_reasons=failed)


def status(assessment):
    if "FAIL" in (assessment.physical_validity, assessment.design_constraints, assessment.technical_qc):
        return "FAIL"
    return "PASS" if assessment.review_eligible else "UNKNOWN"


def gate_values(pose, policy):
    def limit(value, maximum):
        return value <= maximum if finite(value) else None
    def window(value, bounds):
        return bounds[0] <= value <= bounds[1] if finite(value) else None
    return {
        "scaffold_rmsd": limit(pose.scaffold_rmsd_angstrom, policy.max_scaffold_rmsd_angstrom),
        "interaction_retention": (pose.interaction_retention_fraction >= policy.min_interaction_retention_fraction
                                  if finite(pose.interaction_retention_fraction) and pose.protected_interactions else None),
        "covalent_cs_distance": window(pose.covalent_cs_bond_distance_angstrom, policy.covalent_cs_bond_window_angstrom),
        "attack_angle": window(pose.attack_angle_s_cbeta_calpha_degrees, policy.attack_angle_window_degrees),
        "actual_target_cs_distance": window(pose.actual_sg_cbeta_distance_angstrom, policy.covalent_cs_bond_window_angstrom),
        "actual_target_product_angle": window(pose.actual_sg_cbeta_calpha_angle_degrees, policy.attack_angle_window_degrees),
        "dummy_target_alignment": limit(pose.dummy_to_target_sg_distance_angstrom, policy.max_dummy_to_target_sg_distance_angstrom),
        "ligand_strain": limit(pose.strain_excess_over_parent_kcal_mol, policy.max_strain_excess_over_parent_kcal_mol),
        "protein_clash": limit(pose.clash_fraction, policy.max_clash_fraction),
        "severe_protein_clash_absent": limit(pose.severe_clash_pair_count, policy.max_severe_clash_pair_count),
    }


class PoseEvaluationV2(PoseGeometryEvaluation):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    status: Status
    interaction_retention_fraction: float | None = Field(ge=0, le=1)
    ligand_strain_kcal_mol: float | None = Field(default=None, ge=0)
    parent_ligand_strain_kcal_mol: float | None = Field(default=None, ge=0)
    gate_flags: dict[str, bool | None]
    assessment: Assessment
    strain_context: StrainContext

    @model_validator(mode="after")
    def consistent(self):
        context = self.strain_context
        candidate, parent = self.ligand_strain_kcal_mol, self.parent_ligand_strain_kcal_mol
        available = finite(candidate) and finite(parent)
        same = context.candidate_forcefield is not None and context.candidate_forcefield == context.parent_forcefield
        expected = "UNAVAILABLE" if not available else "AVAILABLE" if same else "METHOD_MISMATCH"
        if (context.comparison_status != expected or context.candidate_forcefield != self.strain_forcefield
                or context.candidate_converged != finite(candidate) or context.parent_converged != finite(parent)
                or self.strain_estimator != context.estimator
                or (finite(candidate) and context.candidate_forcefield is None)
                or (finite(parent) and context.parent_forcefield is None)):
            raise ValueError("Inconsistent local relaxation provenance")
        delta = candidate - parent if available and same else None
        if self.strain_excess_over_parent_kcal_mol != delta:
            raise ValueError("Relative local energy requires comparable measured references")
        if any(not finite(item.distance_angstrom) or not finite(item.maximum_distance_angstrom)
               or item.retained != (item.distance_angstrom <= item.maximum_distance_angstrom)
               for item in self.protected_interactions):
            raise ValueError("Protected-contact labels differ from measured distances")
        retention = (sum(item.retained for item in self.protected_interactions) / len(self.protected_interactions)
                     if self.protected_interactions else None)
        if self.interaction_retention_fraction != retention:
            raise ValueError("Empty protected contacts have unknown retention, not zero")
        expected_assessment = assess(self.gate_flags)
        if self.assessment != expected_assessment or self.status != status(expected_assessment):
            raise ValueError("Pose independent axes differ from constraint evidence")
        if self.failure_reasons != expected_assessment.failure_reasons:
            raise ValueError("Missing measurements cannot be listed as measured failures")
        return self


def make_pose(legacy, policy, parent_forcefield):
    values = legacy.model_dump()
    if not legacy.protected_interactions:
        values["interaction_retention_fraction"] = None
    for name in ("carsidock_score", "pose_loss", "pose_mdn_score"):
        if not finite(values[name]):
            values[name] = None
    flags = gate_values(legacy, policy)
    assessment = assess(flags)
    candidate, parent = legacy.ligand_strain_kcal_mol, legacy.parent_ligand_strain_kcal_mol
    context = StrainContext(candidate_forcefield=legacy.strain_forcefield, parent_forcefield=parent_forcefield,
        candidate_converged=finite(candidate), parent_converged=finite(parent),
        comparison_status=("UNAVAILABLE" if not (finite(candidate) and finite(parent))
                           else "AVAILABLE" if legacy.strain_forcefield == parent_forcefield else "METHOD_MISMATCH"),
        rdkit_version=rdBase.rdkitVersion)
    values.update(status=status(assessment), gate_flags=flags, failure_reasons=assessment.failure_reasons,
                  assessment=assessment, strain_context=context)
    return PoseEvaluationV2(**values)


def ensemble_assessment(poses, count, errors):
    complete = bool(count) and len(poses) == count and not errors and all(p.assessment.evidence_complete for p in poses)
    def aggregate(name):
        values = [getattr(p.assessment, name) for p in poses]
        if not values:
            return "NOT_EVALUATED"
        if "PASS" in values:
            return "PASS"
        return "UNKNOWN" if len(poses) != count or errors or "UNKNOWN" in values else "FAIL"
    missing = ([f"evaluation_error:{error}" for error in errors]
               + (["pose_records_unavailable"] if not count or len(poses) != count else [])
               + [f"pose:{p.pose_index}:{reason}" for p in poses for reason in p.assessment.missing_reasons])
    return Assessment(physical_validity=aggregate("physical_validity"),
        design_constraints=aggregate("design_constraints"), technical_qc=aggregate("technical_qc"),
        evidence_complete=complete, review_eligible=bool(count) and len(poses) == count and not errors
            and any(p.assessment.review_eligible for p in poses),
        missing_reasons=missing,
        failure_reasons=[f"pose:{p.pose_index}:{reason}" for p in poses for reason in p.failure_reasons])


def ensemble_status(poses, assessment):
    if not poses:
        return "NOT_EVALUATED"
    if assessment.review_eligible:
        return "PASS"
    return "UNKNOWN" if not assessment.evidence_complete else "FAIL"


class GeometryResultV2(LigandFirstGeometryResult):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal["2.0"] = "2.0"
    status: Status
    policy: ProvisionalGeometryPolicy
    poses: list[PoseEvaluationV2]
    selected_pose: PoseEvaluationV2 | None = None
    assessment: Assessment
    aggregation: Literal["any_pose_per_axis_joint_eligibility_requires_one_complete_pose_and_no_evaluation_errors"] = "any_pose_per_axis_joint_eligibility_requires_one_complete_pose_and_no_evaluation_errors"

    @model_validator(mode="after")
    def consistent(self):
        indexes = [pose.pose_index for pose in self.poses]
        if len(set(indexes)) != len(indexes) or any(i >= self.pose_count for i in indexes):
            raise ValueError("Pose identities/counts differ")
        for pose in self.poses:
            if pose.gate_flags != gate_values(pose, self.policy):
                raise ValueError("Pose gates differ from frozen policy and measurements")
        assessment = ensemble_assessment(self.poses, self.pose_count, self.errors)
        if self.assessment != assessment or self.status != ensemble_status(self.poses, assessment):
            raise ValueError("Ensemble assessment differs from measured poses")
        passed = [pose for pose in self.poses if pose.assessment.review_eligible]
        if (self.passing_pose_count != len(passed) or self.ensemble_pass_fraction !=
                (len(passed) / self.pose_count if self.pose_count else 0.0)):
            raise ValueError("Pose pass counts differ")
        selected = min(passed, key=lambda p: (p.carsidock_score if p.carsidock_score is not None else float("inf"),
                       p.scaffold_rmsd_angstrom, p.pose_index), default=None) if assessment.review_eligible else None
        if self.selected_pose != selected or self.selected_pose_index != (selected.pose_index if selected else None):
            raise ValueError("Selected pose must follow complete evidence and the frozen ordering")
        return self
