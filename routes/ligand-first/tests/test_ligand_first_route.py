import json
from pathlib import Path

from covscreen.ligand_first import LigandFirstRequest, plan_ligand_first
from covscreen.research import CovalentAssessment, RoutePlan, TargetPassport
from covscreen.tool_api import plan_ligand_first_tool


ROOT = (Path(__file__).resolve().parents[3] / "science")


def load_state(project_id="egfr-c797"):
    project = ROOT / "projects" / project_id
    passport = TargetPassport.model_validate(json.loads((project / "target_passport.json").read_text()))
    assessment = CovalentAssessment.model_validate(
        json.loads((project / "assessment/covalent_assessment.json").read_text())
    )
    route = RoutePlan.model_validate(json.loads((project / "plans/route_plan.json").read_text()))
    return passport, assessment, route


def complete_request():
    return LigandFirstRequest.model_validate(
        json.loads(
            (ROOT / "projects/egfr-c797/plans/ligand_first_request.example.json").read_text()
        )
    )


def test_documented_ligand_first_route_reaches_generation_gate():
    passport, assessment, route = load_state()
    plan = plan_ligand_first(complete_request(), passport, assessment, route)
    stage_ids = [stage.stage_id for stage in plan.stages]
    assert plan.decision == "READY_FOR_GENERATION"
    assert stage_ids == [
        "project-and-route-gate",
        "noncovalent-pose-acquisition",
        "scaffold-modification-map",
        "attachment-vector-linker-warhead-plan",
        "warhead-linker-generation",
        "covalent-pose-geometry-screen",
        "candidate-pool-handoff",
    ]
    assert plan.eligible_attachment_ranks
    generation = plan.stages[4]
    assert generation.tools == ["covalentizer", "rdkit"]
    assert generation.status == "planned"
    assert generation.requires_human_approval is True
    assert plan.stages[5].tools == ["CarsiDock-Cov"]


def test_missing_pose_stops_before_scaffold_and_attachment_approval():
    passport, assessment, route = load_state()
    request = complete_request()
    request = request.model_copy(
        update={
            "attachment": request.attachment.model_copy(
                update={"pose_source": "none", "pose_reference": None}
            )
        }
    )
    plan = plan_ligand_first(request, passport, assessment, route)
    assert plan.decision == "NEEDS_NONCOVALENT_POSE"
    assert plan.stages[1].status == "ready"
    assert plan.stages[2].status == "blocked"
    assert plan.attachment_plan.status == "NEEDS_STRUCTURE"


def test_generated_candidates_advance_only_to_covalent_screen():
    passport, assessment, route = load_state()
    plan = plan_ligand_first(
        complete_request(),
        passport,
        assessment,
        route,
        generated_candidates_available=True,
    )
    assert plan.decision == "READY_FOR_COVALENT_SCREEN"
    assert plan.stages[4].status == "completed"
    assert plan.stages[5].status == "planned"
    assert plan.stages[6].status == "blocked"


def test_screened_candidates_reach_unified_pool_handoff():
    passport, assessment, route = load_state()
    plan = plan_ligand_first(
        complete_request(),
        passport,
        assessment,
        route,
        generated_candidates_available=True,
        covalent_screen_results_available=True,
    )
    assert plan.decision == "READY_FOR_POOL_HANDOFF"
    assert plan.stages[5].status == "completed"
    assert plan.stages[6].status == "ready"


def test_project_tool_reads_project_local_request():
    result = plan_ligand_first_tool(
        ROOT,
        "projects/egfr-c797/plans/ligand_first_request.example.json",
    )
    assert result["status"] == "PASS"
    assert result["plan"]["decision"] == "READY_FOR_GENERATION"
