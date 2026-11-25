import json
from pathlib import Path

from covscreen.de_novo import (
    DeNovoGeneratorAvailability,
    DeNovoRequest,
    plan_de_novo,
)
from covscreen.research import CovalentAssessment, RoutePlan, TargetPassport
from covscreen.tool_api import plan_de_novo_tool


ROOT = (Path(__file__).resolve().parents[3]/"science")


def load_state():
    project = ROOT / "projects/egfr-c797"
    passport = TargetPassport.model_validate(json.loads((project / "target_passport.json").read_text()))
    assessment = CovalentAssessment.model_validate(
        json.loads((project / "assessment/covalent_assessment.json").read_text())
    )
    route = RoutePlan.model_validate(json.loads((project / "plans/route_plan.json").read_text()))
    request = DeNovoRequest.model_validate(
        json.loads((project / "plans/de_novo_request.example.json").read_text())
    )
    return passport, assessment, route, request


def all_available():
    return DeNovoGeneratorAvailability(
        pocketxmol=True,
        covalentizer_rdkit=True,
        rdkit=True,
        reinvent4=True,
        difflinker=True,
        carsidock_cov=True,
    )


def test_route_has_all_documented_stages_and_generation_gate():
    passport, assessment, route, request = load_state()
    plan = plan_de_novo(request, passport, assessment, route, all_available())
    assert plan.decision == "READY_FOR_SCAFFOLD_GENERATION"
    assert [stage.stage_id for stage in plan.stages] == [
        "project-and-route-gate",
        "pocket-definition",
        "pocket-conditioned-3d-scaffold-generation",
        "target-residue-warhead-constrained-generation",
        "linker-rgroup-multiobjective-optimization",
        "carsidock-cov-pose-screen",
        "candidate-pool-handoff",
    ]
    assert plan.stages[2].tools == ["pocketxmol"]
    assert plan.stages[3].tools == ["covalentizer_rdkit", "RDKit SMARTS"]
    assert plan.stages[4].tools == ["reinvent4", "difflinker"]
    assert all(plan.stages[index].requires_human_approval for index in (2, 3, 4, 5))


def test_missing_generator_assets_are_reported_without_false_execution():
    passport, assessment, route, request = load_state()
    plan = plan_de_novo(request, passport, assessment, route, DeNovoGeneratorAvailability())
    assert plan.decision == "NEEDS_GENERATOR_ASSETS"
    assert set(plan.missing_assets) == {
        "pocketxmol", "covalentizer_rdkit", "rdkit", "reinvent4", "difflinker", "carsidock_cov"
    }
    assert plan.stages[2].status == "blocked"


def test_incomplete_pocket_precedes_asset_gate():
    passport, assessment, route, request = load_state()
    request = request.model_copy(update={"pocket": request.pocket.model_copy(update={"center_xyz_angstrom": None})})
    plan = plan_de_novo(request, passport, assessment, route, DeNovoGeneratorAvailability())
    assert plan.decision == "NEEDS_POCKET_DEFINITION"
    assert plan.stages[1].status == "ready"
    assert plan.stages[2].status == "blocked"


def test_stage_artifacts_advance_in_order_to_pool_handoff():
    passport, assessment, route, request = load_state()
    plan = plan_de_novo(
        request,
        passport,
        assessment,
        route,
        all_available(),
        scaffold_candidates_available=True,
        covalent_candidates_available=True,
        optimized_candidates_available=True,
        covalent_screen_results_available=True,
    )
    assert plan.decision == "READY_FOR_POOL_HANDOFF"
    assert [stage.status for stage in plan.stages[2:6]] == ["completed"] * 4
    assert plan.stages[6].status == "ready"


def test_multiobjective_policy_keeps_anti_reward_hacking_evidence():
    _, _, _, request = load_state()
    objectives = request.objectives
    assert objectives.require_score_component_logging
    assert objectives.require_structure_alert_reasons
    assert objectives.require_duplicate_control
    assert objectives.reactivity_risk_penalty > 0
    assert objectives.max_molecular_weight <= 650


def test_tool_marks_supplied_generator_availability_as_planning_only():
    # Supplied flags model a scenario; this tool cannot authenticate their origin.
    result = plan_de_novo_tool(
        ROOT,
        "projects/egfr-c797/plans/de_novo_request.example.json",
        availability=all_available().model_dump(mode="json"),
    )
    assert result["status"] == "PASS"
    assert result["plan"]["decision"] == "READY_FOR_SCAFFOLD_GENERATION"
    assert result["plan"]["missing_assets"] == []
    assert result["availability_source"] == "caller_asserted"
    assert result["planning_only"] is True
    assert result["compute_submitted"] is False
    assert result["assessment"]["state"] == "not_evaluated"


def test_missing_pocket_blocks_every_downstream_stage_even_with_artifacts():
    passport, assessment, route, request = load_state()
    request = request.model_copy(update={'pocket': request.pocket.model_copy(update={'center_xyz_angstrom': None})})
    plan = plan_de_novo(request, passport, assessment, route, all_available(),
        scaffold_candidates_available=True, covalent_candidates_available=True,
        optimized_candidates_available=True, covalent_screen_results_available=True)
    assert plan.decision == 'NEEDS_POCKET_DEFINITION'
    assert all(stage.status == 'blocked' for stage in plan.stages[2:])


def test_missing_stage_parent_cannot_be_bypassed_by_later_artifacts():
    passport, assessment, route, request = load_state()
    plan = plan_de_novo(request, passport, assessment, route, all_available(),
        scaffold_candidates_available=True, covalent_candidates_available=False,
        optimized_candidates_available=True, covalent_screen_results_available=True)
    assert plan.decision == 'READY_FOR_WARHEAD_CONSTRAINT'
    assert plan.stages[3].status == 'ready'
    assert all(stage.status == 'blocked' for stage in plan.stages[4:])


def test_unavailable_assets_cannot_leave_handoff_ready():
    passport, assessment, route, request = load_state()
    plan = plan_de_novo(request, passport, assessment, route, DeNovoGeneratorAvailability(),
        scaffold_candidates_available=True, covalent_candidates_available=True,
        optimized_candidates_available=True, covalent_screen_results_available=True)
    assert plan.decision == 'NEEDS_GENERATOR_ASSETS'
    assert all(stage.status == 'blocked' for stage in plan.stages[2:])


def test_existing_unverified_files_never_establish_stage_completion(tmp_path):
    passport, assessment, route, request = load_state()
    project = tmp_path / 'projects' / request.project_id
    for name, value in [('target_passport.json', passport),
                        ('assessment/covalent_assessment.json', assessment),
                        ('plans/route_plan.json', route)]:
        destination = project / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(value.model_dump_json())
    data = request.model_dump(mode='json')
    for field in ['scaffold_candidates_path', 'covalent_candidates_path',
                  'optimized_candidates_path', 'covalent_screen_results_path']:
        data[field] = 'plans/' + field + '.txt'
        (project / data[field]).write_text('NOT A MOLECULE OR VALIDATED RESULT')
    destination = project / 'plans/request.json'
    destination.write_text(json.dumps(data))
    for require_inputs in (True, False):
        result = plan_de_novo_tool(tmp_path, str(destination.relative_to(tmp_path)),
            require_inputs=require_inputs, availability=all_available().model_dump())
        assert result['status'] == 'FAIL'
        assert len(result['validation']['errors']) == 4
        assert all('unverified stage artifact' in error for error in result['validation']['errors'])
        assert 'plan' not in result
