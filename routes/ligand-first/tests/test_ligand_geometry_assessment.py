"""Versioned QC on recorded poses; modified values below test contracts, not drugs."""
import json

import pytest
from covscreen.ligand_first_geometry import (
    evaluate_carsidock_geometry, load_sdf_poses, parse_geometry_policy, provisional_geometry_policy,
)
from covscreen.ligand_first_inputs import load_campaign, build_parent_pose_mol
from covscreen.ligand_pose_evidence import incomplete
from covscreen_ligand_first.ligand_geometry_assessment import (
    GeometryResultV2, make_pose, ensemble_assessment, ensemble_status, assess,
)
from test_ligand_first_geometry import CAMPAIGN_PATH, GEOMETRY_DIR


@pytest.fixture(scope="module")
def inputs():
    campaign = load_campaign(CAMPAIGN_PATH)
    return dict(campaign=campaign, parent_pose=build_parent_pose_mol(campaign, CAMPAIGN_PATH.parent),
        candidate_id="recorded-candidate", carsidock_run_id="cpu-remeasurement-not-new-docking",
        raw_poses=load_sdf_poses(GEOMETRY_DIR / "carsidock.sdf"),
        no_dummy_poses=load_sdf_poses(GEOMETRY_DIR / "carsidock_nodumat.sdf"),
        receptor_pdb=CAMPAIGN_PATH.parent / campaign.parent_pose.structure_path)


@pytest.fixture(scope="module")
def legacy(inputs):
    return evaluate_carsidock_geometry(**inputs)


@pytest.fixture(scope="module")
def strict(inputs):
    return evaluate_carsidock_geometry(**inputs, policy=provisional_geometry_policy())


def result(pose, policy, errors=None, count=1):
    assessment = ensemble_assessment([pose], count, errors or [])
    selected = pose if assessment.review_eligible else None
    return GeometryResultV2(campaign_id="software", candidate_id="software", carsidock_run_id="software",
        status=ensemble_status([pose], assessment), pose_count=count,
        passing_pose_count=int(pose.assessment.review_eligible),
        ensemble_pass_fraction=int(pose.assessment.review_eligible) / count,
        selected_pose_index=selected.pose_index if selected else None, selected_pose=selected,
        poses=[pose], policy=policy, errors=errors or [], assessment=assessment)


def test_real_ensemble_versions_preserve_measurements_and_separate_axes(legacy, strict):
    assert legacy.schema_version == "1.0" and strict.schema_version == "2.0"
    assert "version" not in legacy.policy.model_dump()
    assert "assessment" not in legacy.model_dump()
    assert "strain_context" not in legacy.poses[0].model_dump()
    assert strict.pose_count == legacy.pose_count == 9
    assert not strict.errors and strict.assessment.evidence_complete
    assert strict.selected_pose_index == legacy.selected_pose_index
    assert strict.passing_pose_count == legacy.passing_pose_count
    for first, second in zip(legacy.poses, strict.poses):
        assert first.strain_excess_over_parent_kcal_mol == second.strain_excess_over_parent_kcal_mol
        assert first.scaffold_rmsd_angstrom == second.scaffold_rmsd_angstrom
        assert first.gate_flags == second.gate_flags
        context = second.strain_context
        assert context.comparison_status == "AVAILABLE"
        assert context.candidate_forcefield == context.parent_forcefield == first.strain_forcefield
        assert context.candidate_converged and context.parent_converged
        assert context.reference_state == "same_molecule_single_start_local_minimum"
    assert GeometryResultV2.model_validate_json(strict.model_dump_json()) == strict
    assert not incomplete(strict.model_dump(mode="json"))


def test_missing_strain_is_unknown_design_evidence_not_physical_failure(legacy):
    original = legacy.poses[0]
    measured = original.model_copy(update={"ligand_strain_kcal_mol": None,
        "strain_excess_over_parent_kcal_mol": None})
    policy = provisional_geometry_policy()
    pose = make_pose(measured, policy, original.strain_forcefield)
    assert pose.status == "UNKNOWN"
    assert pose.assessment.physical_validity == "PASS"
    assert pose.assessment.design_constraints == "UNKNOWN"
    assert not pose.assessment.review_eligible and not pose.assessment.evidence_complete
    assert pose.gate_flags["ligand_strain"] is None
    assert pose.failure_reasons == []
    assert pose.assessment.missing_reasons == ["ligand_strain"]
    value = result(pose, policy)
    assert value.status == "UNKNOWN" and value.selected_pose is None
    assert incomplete(value.model_dump(mode="json"))


def test_empty_contacts_are_null_and_not_measured_loss(legacy):
    policy = provisional_geometry_policy()
    measured = legacy.poses[0].model_copy(update={"protected_interactions": [],
                                               "interaction_retention_fraction": 0})
    pose = make_pose(measured, policy, measured.strain_forcefield)
    assert pose.interaction_retention_fraction is None
    assert pose.gate_flags["interaction_retention"] is None
    assert pose.assessment.missing_reasons == ["interaction_retention"]
    assert pose.assessment.physical_validity == "PASS" and pose.status == "UNKNOWN"


def test_measured_contact_loss_is_a_design_failure(legacy):
    original = legacy.poses[0]
    interactions = [item.model_copy(update={"distance_angstrom": item.maximum_distance_angstrom + 1,
                                           "retained": False}) for item in original.protected_interactions]
    pose = make_pose(original.model_copy(update={"protected_interactions": interactions,
        "interaction_retention_fraction": 0}), provisional_geometry_policy(), original.strain_forcefield)
    assert pose.status == "FAIL" and pose.assessment.evidence_complete
    assert pose.assessment.physical_validity == "PASS"
    assert pose.assessment.design_constraints == "FAIL"
    assert pose.failure_reasons == ["interaction_retention"]


def test_known_physical_failure_survives_missing_energy(legacy):
    original = legacy.poses[0]
    pose = make_pose(original.model_copy(update={"actual_sg_cbeta_distance_angstrom": 8,
        "ligand_strain_kcal_mol": None, "strain_excess_over_parent_kcal_mol": None}),
        provisional_geometry_policy(), original.strain_forcefield)
    assert pose.status == "FAIL"
    assert pose.assessment.physical_validity == "FAIL"
    assert pose.assessment.design_constraints == "UNKNOWN"
    assert not pose.assessment.evidence_complete
    assert pose.failure_reasons == ["actual_target_cs_distance"]


def test_different_forcefields_withhold_relative_energy(legacy):
    original = legacy.poses[0]
    pose = make_pose(original.model_copy(update={"strain_excess_over_parent_kcal_mol": None}),
        provisional_geometry_policy(), "UFF" if original.strain_forcefield == "MMFF94s" else "MMFF94s")
    assert pose.strain_context.comparison_status == "METHOD_MISMATCH"
    assert pose.strain_context.candidate_converged and pose.strain_context.parent_converged
    assert pose.gate_flags["ligand_strain"] is None
    with pytest.raises(ValueError, match="comparable"):
        make_pose(original, provisional_geometry_policy(), pose.strain_context.parent_forcefield)


def test_zero_relative_energy_is_a_real_measurement(legacy):
    original = legacy.poses[0]
    pose = make_pose(original.model_copy(update={"ligand_strain_kcal_mol": original.parent_ligand_strain_kcal_mol,
        "strain_excess_over_parent_kcal_mol": 0}), provisional_geometry_policy(), original.strain_forcefield)
    assert pose.gate_flags["ligand_strain"] is True
    assert pose.assessment.review_eligible


def test_incomplete_ensemble_cannot_hide_unmeasured_slots(strict):
    pose = strict.poses[0]
    value = result(pose, strict.policy, errors=["pose 1: missing atom map"], count=2)
    assert value.assessment.physical_validity == "PASS"
    assert not value.assessment.evidence_complete and not value.assessment.review_eligible
    assert value.status == "UNKNOWN" and value.selected_pose is None


def test_no_pose_is_not_evaluated_not_physically_invalid(inputs):
    value = evaluate_carsidock_geometry(**{**inputs, "raw_poses": [], "no_dummy_poses": []},
                                       policy=provisional_geometry_policy())
    assert value.status == "NOT_EVALUATED"
    assert value.assessment.physical_validity == "NOT_EVALUATED"
    assert not value.assessment.evidence_complete and not value.assessment.review_eligible


@pytest.mark.parametrize("change", ["axes", "gate", "selection", "count", "method", "delta", "contacts"])
def test_altered_assessments_cannot_survive_roundtrip(strict, change):
    value = json.loads(strict.model_dump_json())
    pose = value["poses"][0]
    if change == "axes":
        pose["assessment"]["physical_validity"] = "FAIL"
    elif change == "gate":
        pose["gate_flags"]["actual_target_cs_distance"] = False
        pose["assessment"] = assess(pose["gate_flags"]).model_dump()
        pose["status"] = "FAIL"
        pose["failure_reasons"] = pose["assessment"]["failure_reasons"]
    elif change == "selection":
        value["selected_pose_index"] = 8
    elif change == "count":
        value["passing_pose_count"] += 1
    elif change == "method":
        pose["strain_context"]["parent_forcefield"] = None
    elif change == "delta":
        pose["strain_excess_over_parent_kcal_mol"] = 0
    else:
        pose["protected_interactions"][0]["retained"] = False
    with pytest.raises(ValueError):
        GeometryResultV2.model_validate(value)


def test_policy_versions_are_explicit_and_unknown_versions_rejected(legacy):
    assert parse_geometry_policy(legacy.policy.model_dump()).model_dump() == legacy.policy.model_dump()
    value = provisional_geometry_policy().model_dump(mode="json")
    assert parse_geometry_policy(value).model_dump(mode="json") == value
    with pytest.raises(ValueError, match="Unsupported"):
        parse_geometry_policy({**value, "version": "unknown"})
    with pytest.raises(ValueError):
        parse_geometry_policy({**value, "calibration_status": "calibrated"})


def test_report_rows_preserve_unknowns_and_provenance(legacy):
    from covscreen.ligand_pose_report import measurement_rows
    original = legacy.poses[0]
    pose = make_pose(original.model_copy(update={"ligand_strain_kcal_mol": None,
        "strain_excess_over_parent_kcal_mol": None}), provisional_geometry_policy(), original.strain_forcefield)
    rows = measurement_rows(pose.model_dump(mode="json"), provisional_geometry_policy().model_dump())
    strain = next(row for row in rows if row["legacy_field"] == "strain_excess_over_parent_kcal_mol")
    assert strain["value"] is None and strain["measurement_status"] == "unavailable"
    assert strain["gate_passed"] is None and strain["units"] == "kcal/mol"


def test_handoff_revalidates_axes_instead_of_trusting_pass_label(strict):
    from covscreen.ligand_pose_evidence import review_eligible
    value = json.loads(strict.model_dump_json())
    assert review_eligible(value)
    value["assessment"]["evidence_complete"] = False
    with pytest.raises(ValueError):
        review_eligible(value)


def test_nonfinite_scores_never_win_pose_selection(inputs):
    from rdkit import Chem
    raw = [Chem.Mol(pose) for pose in inputs["raw_poses"]]
    raw[0].SetProp("CarsiScore", "-inf")
    value = evaluate_carsidock_geometry(**{**inputs, "raw_poses": raw},
                                       policy=provisional_geometry_policy())
    assert value.poses[0].carsidock_score is None
    assert value.selected_pose_index != 0
    value.model_dump_json()


def test_nonfinite_coordinates_become_evaluation_gap(inputs):
    from rdkit import Chem
    raw = Chem.Mol(inputs["raw_poses"][0])
    raw.GetConformer().SetAtomPosition(0, (float("nan"), 0, 0))
    value = evaluate_carsidock_geometry(**{**inputs, "raw_poses": [raw],
        "no_dummy_poses": inputs["no_dummy_poses"][:1]}, policy=provisional_geometry_policy())
    assert value.status == "NOT_EVALUATED" and value.errors
    assert value.assessment.physical_validity == "NOT_EVALUATED"
    assert not value.assessment.review_eligible


def test_policy_rejects_nonfinite_thresholds():
    with pytest.raises(ValueError):
        provisional_geometry_policy().__class__(max_strain_excess_over_parent_kcal_mol=float("inf"))


def test_missing_gate_cannot_be_interpreted_as_a_complete_review(strict):
    value = json.loads(strict.model_dump_json())
    del value["poses"][0]["gate_flags"]["ligand_strain"]
    with pytest.raises(ValueError, match="constraint keys"):
        GeometryResultV2.model_validate(value)


def test_negative_absolute_relaxation_energy_is_rejected(strict):
    value = json.loads(strict.model_dump_json())
    value["poses"][0]["ligand_strain_kcal_mol"] = -1
    with pytest.raises(ValueError):
        GeometryResultV2.model_validate(value)


def test_other_pose_missing_energy_does_not_erase_one_complete_jointly_passing_pose(legacy):
    from covscreen.ligand_pose_evidence import review_eligible
    policy = provisional_geometry_policy()
    first = make_pose(legacy.poses[0], policy, legacy.poses[0].strain_forcefield)
    second = make_pose(legacy.poses[1].model_copy(update={"ligand_strain_kcal_mol": None,
        "strain_excess_over_parent_kcal_mol": None}), policy, legacy.poses[1].strain_forcefield)
    assessment = ensemble_assessment([first, second], 2, [])
    assert not assessment.evidence_complete
    assert assessment.review_eligible and assessment.missing_reasons == ["pose:1:ligand_strain"]
    value = GeometryResultV2(campaign_id="software", candidate_id="software", carsidock_run_id="software",
        status="PASS", pose_count=2, passing_pose_count=1, ensemble_pass_fraction=.5,
        selected_pose_index=first.pose_index, selected_pose=first, poses=[first, second],
        policy=policy, errors=[], assessment=assessment)
    assert incomplete(value.model_dump(mode="json"))
    assert review_eligible(value.model_dump(mode="json"))
