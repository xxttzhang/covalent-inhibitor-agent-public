from covscreen.cascade import (
    CascadeObservation,
    CascadePolicy,
    EngineAvailability,
    choose_next_action,
    plan_library_first_cascade,
)
from covscreen.research import (
    CovalentAssessment,
    LibraryFirstRequest,
    RoutePlan,
)


def request():
    return LibraryFirstRequest(
        request_id="req-1",
        project_id="example-cys",
        target_passport_path="target_passport.json",
        receptor_path="structures/receptor.pdb",
        ligand_table_path="library/input.csv",
        site_id="EXAMPLE_C100",
        chain_id="A",
        residue_name="CYS",
        residue_number=100,
        reactive_atom="SG",
        reaction_type="Michael Addition",
        output_dir="runs/req-1",
        engines=[
            {"engine": "carsidock-cov"},
            {"engine": "carsidock-cov-rtmscore"},
            {"engine": "alphafold3-mpae"},
            {"engine": "schrodinger-covdock-lo"},
        ],
        resource_budget={
            "max_gpu_hours": 200,
            "max_wall_hours": 48,
            "max_concurrent_jobs": 2,
        },
    )


def assessment(decision="GO"):
    return CovalentAssessment(
        project_id="example-cys",
        decision=decision,
        evidence_coverage=1,
        support_score=1,
        criteria=[],
        blockers=[] if decision != "STOP" else ["site mismatch"],
        uncertainties=[],
    )


def route(selected="library-first"):
    return RoutePlan(
        project_id="example-cys",
        selected_route=selected,
        decision="GO",
        rationale=["test"],
        prerequisites=[],
        alternatives=[],
        stop_conditions=[],
    )


def availability():
    return EngineAvailability(
        carsidock_cov=True,
        rtmscore=True,
        alphafold3_mpae=True,
        covdock_vs=True,
        covdock_lo=True,
        schrodinger_license_ok=True,
    )


def test_cascade_is_staged_and_caps_expensive_engines():
    plan = plan_library_first_cascade(
        request(),
        assessment(),
        route(),
        10_000,
        availability(),
        CascadePolicy(allow_proprietary=True),
    )
    stages = {stage.stage_id: stage for stage in plan.stages}
    assert plan.decision in {"READY", "CONDITIONAL"}
    assert stages["catalog-qc-prescreen"].target_output_count == 10_000
    assert stages["carsidock-cov"].max_candidates == 10_000
    assert stages["rtmscore"].max_candidates == 10_000
    assert stages["recall-pool"].target_output_count == 2_000
    assert stages["alphafold3-mpae"].max_candidates == 2_000
    assert stages["alphafold3-mpae"].target_output_count == 100
    assert stages["covdock-lo-gate"].max_candidates == 100
    assert stages["pareto-finalize"].target_output_count == 30
    assert "schrodinger-covdock-lo" in stages["covdock-lo-gate"].engines


def test_proprietary_stage_is_skipped_without_policy_approval():
    plan = plan_library_first_cascade(
        request(), assessment(), route(), 1000, availability(), CascadePolicy()
    )
    stage = {item.stage_id: item for item in plan.stages}["covdock-lo-gate"]
    assert stage.status == "skipped"
    assert stage.engines == []


def test_wrong_route_blocks_without_explicit_override():
    plan = plan_library_first_cascade(
        request(), assessment(), route("ligand-first"), 100, availability()
    )
    assert plan.decision == "BLOCKED"
    assert plan.stages == []


def test_stop_assessment_stops_before_compute():
    plan = plan_library_first_cascade(
        request(), assessment("STOP"), route(), 100, availability()
    )
    assert plan.decision == "STOP"
    assert plan.stages == []


def test_failure_denominator_gate_prevents_escalation():
    plan = plan_library_first_cascade(request(), assessment(), route(), 100, availability())
    action = choose_next_action(
        plan,
        CascadeObservation(
            completed_stage_id="carsidock-cov",
            input_count=100,
            success_count=70,
            failed_count=30,
            remaining_gpu_hours=500,
            explicit_failure_table=True,
        ),
    )
    assert action.action == "repair_inputs"


def test_recall_pool_escalates_only_bounded_af3_subset():
    plan = plan_library_first_cascade(request(), assessment(), route(), 1000, availability())
    action = choose_next_action(
        plan,
        CascadeObservation(
            completed_stage_id="recall-pool",
            input_count=1000,
            success_count=950,
            failed_count=50,
            remaining_gpu_hours=500,
            explicit_failure_table=True,
        ),
    )
    assert action.action == "run"
    assert action.stage_id == "alphafold3-mpae"
    assert action.max_candidates <= 2_000
