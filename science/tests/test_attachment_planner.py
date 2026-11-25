import json
from pathlib import Path

import pytest

from covscreen.attachment import AttachmentRequest, plan_attachments
from covscreen.research import CovalentAssessment, RoutePlan, TargetPassport


ROOT = Path(__file__).resolve().parents[1]


def load_state(project_id):
    project = ROOT / "projects" / project_id
    passport = TargetPassport.model_validate(json.loads((project / "target_passport.json").read_text()))
    assessment = CovalentAssessment.model_validate(
        json.loads((project / "assessment/covalent_assessment.json").read_text())
    )
    route = RoutePlan.model_validate(json.loads((project / "plans/route_plan.json").read_text()))
    return passport, assessment, route


def test_attachment_planner_requires_geometry_before_design_approval():
    passport, assessment, route = load_state("egfr-c797")
    result = plan_attachments(
        AttachmentRequest(
            project_id="egfr-c797",
            ligand_id="contract-fixture",
            ligand_smiles="COc1ccc(NC(C)=O)cc1",
            max_plans=5,
        ),
        passport,
        assessment,
        route,
    )
    assert result.status == "NEEDS_STRUCTURE"
    assert result.candidates
    assert all(candidate.confidence == "low" for candidate in result.candidates)
    assert "hypotheses" in result.disclaimer


def test_attachment_planner_requires_complete_documented_vector_evidence():
    passport, assessment, route = load_state("egfr-c797")
    result = plan_attachments(
        AttachmentRequest(
            project_id="egfr-c797",
            ligand_id="contract-fixture",
            ligand_smiles="CCOc1ccccc1",
            pose_source="experimental",
            pose_reference="PDB:test-fixture",
            scaffold_core_atom_indices=[3, 4, 5, 6, 7, 8],
            key_interaction_atom_indices=[2],
            attachment_atom_indices=[4],
            protected_atom_indices=[2],
            solvent_exposed_atom_indices=[4],
            distance_to_target_angstrom={4: 5.5},
            vector_to_target_angle_degrees={4: 20.0},
            steric_clash_fraction={4: 0.10},
            interaction_retention_fraction={4: 0.85},
            synthetic_accessibility_score={4: 0.75},
            warhead_names=["acrylamide"],
        ),
        passport,
        assessment,
        route,
    )
    assert result.status == "READY"
    assert result.candidates[0].ligand_atom_index == 4
    assert result.candidates[0].confidence == "high"
    assert result.candidates[0].evidence_complete is True
    assert result.candidates[0].linker_necessity == "short_spacer"
    assert result.candidates[0].linker_heavy_atom_range[1] <= 10


def test_distance_alone_does_not_advance_to_generation():
    passport, assessment, route = load_state("egfr-c797")
    result = plan_attachments(
        AttachmentRequest(
            project_id="egfr-c797",
            ligand_id="incomplete-vector",
            ligand_smiles="CCOc1ccccc1",
            pose_source="carsidock",
            pose_reference="runs/parent_pose.sdf",
            attachment_atom_indices=[4],
            solvent_exposed_atom_indices=[4],
            distance_to_target_angstrom={4: 5.5},
        ),
        passport,
        assessment,
        route,
    )
    assert result.status == "NEEDS_STRUCTURE"
    assert result.candidates[0].evidence_complete is False
    assert any("vector angle" in item.lower() for item in result.candidates[0].liabilities)


def test_protected_atom_is_rejected():
    passport, assessment, route = load_state("egfr-c797")
    result = plan_attachments(
        AttachmentRequest(
            project_id="egfr-c797",
            ligand_id="contract-fixture",
            ligand_smiles="CCO",
            attachment_atom_indices=[1],
            protected_atom_indices=[1],
        ),
        passport,
        assessment,
        route,
    )
    assert result.status == "BLOCKED"
    assert result.rejected_atom_indices[1].startswith("explicitly protected")


def test_negative_site_control_stops_attachment_planning():
    passport, assessment, route = load_state("kras-wt-g12-control")
    result = plan_attachments(
        AttachmentRequest(
            project_id="kras-wt-g12-control",
            ligand_id="control",
            ligand_smiles="CCO",
            route_override_approved=True,
        ),
        passport,
        assessment,
        route,
    )
    assert result.status == "STOP"
    assert result.candidates == []


def test_invalid_atom_index_is_rejected():
    passport, assessment, route = load_state("egfr-c797")
    with pytest.raises(ValueError, match="outside ligand range"):
        plan_attachments(
            AttachmentRequest(
                project_id="egfr-c797",
                ligand_id="bad-index",
                ligand_smiles="CCO",
                attachment_atom_indices=[99],
            ),
            passport,
            assessment,
            route,
        )
