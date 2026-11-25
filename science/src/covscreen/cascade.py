from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .research import CovalentAssessment, LibraryFirstRequest, RoutePlan, utc_now


CascadeAction = Literal["run", "repair_inputs", "review", "finalize", "stop"]


class EngineAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    covindb_prescreen: bool = True
    carsidock_cov: bool = False
    rtmscore: bool = False
    recall_pool: bool = True
    alphafold3_mpae: bool = False
    covdock_vs: bool = False
    covdock_lo: bool = False
    schrodinger_license_ok: bool = False
    pareto_selection: bool = True


class CascadePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_failure_fraction: float = Field(default=0.20, ge=0, le=1)
    min_carsidock_success_rate: float = Field(default=0.75, ge=0, le=1)
    min_af3_coverage: float = Field(default=0.95, ge=0, le=1)
    min_covdock_success_rate: float = Field(default=0.80, ge=0, le=1)
    max_prescreen_candidates: int = Field(default=20_000, ge=1)
    max_recall_pool_candidates: int = Field(default=2_000, ge=1)
    max_af3_candidates: int = Field(default=2_000, ge=1)
    max_af3_top_candidates: int = Field(default=100, ge=1)
    max_covdock_lo_candidates: int = Field(default=100, ge=1)
    final_candidate_count: int = Field(default=30, ge=1)
    allow_proprietary: bool = False


class CascadeStage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: int = Field(ge=0)
    stage_id: str
    objective: str
    engines: list[str]
    input_scope: str
    max_candidates: int = Field(ge=0)
    target_output_count: int = Field(default=0, ge=0)
    trigger: str
    estimated_gpu_hours_upper: float = Field(ge=0)
    requires_human_approval: bool
    status: Literal["planned", "skipped", "blocked"] = "planned"
    rationale: list[str]


class AdaptiveCascadePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    request_id: str
    project_id: str
    decision: Literal["READY", "CONDITIONAL", "BLOCKED", "STOP"]
    library_size: int = Field(gt=0)
    stages: list[CascadeStage]
    stop_conditions: list[str]
    invariants: list[str]
    generated_at: str = Field(default_factory=utc_now)
    disclaimer: str = (
        "This cascade allocates computation; it does not establish potency, "
        "selectivity, target engagement, or experimental validation."
    )


class CascadeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed_stage_id: str | None = None
    input_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    rank_disagreement: float | None = Field(default=None, ge=0, le=1)
    geometry_pass_fraction: float | None = Field(default=None, ge=0, le=1)
    unresolved_candidate_count: int = Field(default=0, ge=0)
    remaining_gpu_hours: float = Field(ge=0)
    explicit_failure_table: bool = False
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def counts_balance(self) -> "CascadeObservation":
        if self.success_count + self.failed_count != self.input_count:
            raise ValueError("success_count + failed_count must equal input_count")
        return self


class CascadeNextAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: CascadeAction
    stage_id: str | None = None
    max_candidates: int = Field(ge=0)
    rationale: list[str]
    requires_human_approval: bool


def _stage(
    order: int,
    stage_id: str,
    objective: str,
    engines: list[str],
    input_scope: str,
    max_candidates: int,
    target_output_count: int,
    trigger: str,
    gpu_hours: float,
    rationale: list[str],
    *,
    status: Literal["planned", "skipped", "blocked"] = "planned",
    approval: bool = True,
) -> CascadeStage:
    return CascadeStage(
        order=order,
        stage_id=stage_id,
        objective=objective,
        engines=engines,
        input_scope=input_scope,
        max_candidates=max_candidates,
        target_output_count=target_output_count,
        trigger=trigger,
        estimated_gpu_hours_upper=round(gpu_hours, 3),
        requires_human_approval=approval,
        status=status,
        rationale=rationale,
    )


def plan_library_first_cascade(
    request: LibraryFirstRequest,
    assessment: CovalentAssessment,
    route: RoutePlan,
    library_size: int,
    availability: EngineAvailability,
    policy: CascadePolicy | None = None,
    *,
    route_override_approved: bool = False,
) -> AdaptiveCascadePlan:
    policy = policy or CascadePolicy()
    invariants = [
        "Parse at least 99% of source records and preserve an explicit failure table.",
        "Map the selected warhead for at least 98% of promoted candidates.",
        "Preserve compound_id and input_sdf_index with zero identity loss across every stage.",
        "Keep failures explicit and include them in the original denominator.",
        "Never compare raw scores across engines without direction-aware normalization.",
        "Select the recall pool by declared score-union, scaffold-diversity, and rescue channels.",
        "Use AF3 interface mPAE as the primary rank; use CovDock-LO as a pose gate, not a global score replacement.",
        "Use non-dominated Pareto selection without an undeclared weighted sum.",
        "Do not launch an expensive or proprietary stage without human approval.",
        "Hash inputs, outputs, parameters, model assets, and manifests for every registered run.",
    ]
    stop_conditions = [
        "Target/site identity no longer matches the Target Passport.",
        "The observed failure fraction exceeds the policy threshold without a diagnosed cause.",
        "The remaining resource budget cannot cover the next stage.",
        "Input identities or score direction cannot be recovered unambiguously.",
    ]
    if assessment.decision == "STOP":
        return AdaptiveCascadePlan(
            request_id=request.request_id,
            project_id=request.project_id,
            decision="STOP",
            library_size=library_size,
            stages=[],
            stop_conditions=stop_conditions,
            invariants=invariants,
        )
    if route.selected_route != "library-first" and not route_override_approved:
        return AdaptiveCascadePlan(
            request_id=request.request_id,
            project_id=request.project_id,
            decision="BLOCKED",
            library_size=library_size,
            stages=[],
            stop_conditions=stop_conditions,
            invariants=invariants,
        )

    requested = {item.engine for item in request.engines if item.enabled}
    prescreen_n = min(library_size, policy.max_prescreen_candidates)
    recall_n = min(prescreen_n, policy.max_recall_pool_candidates)
    af3_input_n = min(recall_n, policy.max_af3_candidates)
    af3_top_n = min(af3_input_n, policy.max_af3_top_candidates)
    covdock_n = min(af3_top_n, policy.max_covdock_lo_candidates)
    final_n = min(covdock_n, policy.final_candidate_count)

    carsi_enabled = "carsidock-cov" in requested and availability.carsidock_cov
    rtms_enabled = (
        "carsidock-cov-rtmscore" in requested
        and availability.rtmscore
        and carsi_enabled
    )
    af3_enabled = "alphafold3-mpae" in requested and availability.alphafold3_mpae
    lo_enabled = (
        "schrodinger-covdock-lo" in requested
        and policy.allow_proprietary
        and availability.covdock_lo
        and availability.schrodinger_license_ok
    )

    stages = [
        _stage(
            0,
            "intake",
            "Validate the Target Passport, receptor/site mapping, source identity, and budgets.",
            [],
            "all inputs",
            library_size,
            library_size,
            "always",
            0,
            ["A failed contract blocks every downstream calculation."],
            approval=False,
        ),
        _stage(
            1,
            "catalog-qc-prescreen",
            "Stream CovalentInDB, standardize chemistry, apply warhead/property/alert gates, and retain a target-aware diverse shortlist.",
            ["covindb-prescreen"] if availability.covindb_prescreen else [],
            "validated CovalentInDB records",
            library_size,
            prescreen_n if availability.covindb_prescreen else 0,
            "intake PASS",
            0,
            ["Default full-library budget is Top 20,000; pilot budgets may be smaller."],
            status="planned" if availability.covindb_prescreen else "blocked",
        ),
        _stage(
            2,
            "carsidock-cov",
            "Generate covalent poses and CarsiScore for the complete prescreen shortlist.",
            ["carsidock-cov"] if carsi_enabled else [],
            "stage-two prescreen shortlist",
            prescreen_n if carsi_enabled else 0,
            prescreen_n if carsi_enabled else 0,
            "prescreen quality gates PASS and GPU approval",
            min(request.resource_budget.max_gpu_hours, prescreen_n * 0.03) if carsi_enabled else 0,
            [f"Require CarsiDock success rate >= {policy.min_carsidock_success_rate:.0%}."],
            status="planned" if carsi_enabled else "blocked",
        ),
        _stage(
            3,
            "rtmscore",
            "Rescore existing CarsiDock poses without regenerating conformations.",
            ["carsidock-cov-rtmscore"] if rtms_enabled else [],
            "successful CarsiDock poses only; upstream failures retained",
            prescreen_n if rtms_enabled else 0,
            prescreen_n if rtms_enabled else 0,
            "CarsiDock has traceable poses and an explicit failure table",
            min(request.resource_budget.max_gpu_hours, prescreen_n * 0.005) if rtms_enabled else 0,
            ["RTMScore cannot repair an upstream docking failure."],
            status="planned" if rtms_enabled else "skipped",
        ),
        _stage(
            4,
            "recall-pool",
            "Build the high-recall candidate pool from score union, scaffold diversity, and failure rescue.",
            ["carsidock-pool-selection"] if availability.recall_pool else [],
            "all CarsiDock identities plus optional RTMScore evidence",
            prescreen_n if availability.recall_pool else 0,
            recall_n if availability.recall_pool else 0,
            "CarsiDock quality gate PASS; RTMScore consumed when available",
            0,
            ["Default mix is 70% score union, 20% scaffold diversity, and 10% rescue."],
            status="planned" if availability.recall_pool else "blocked",
        ),
        _stage(
            5,
            "alphafold3-mpae",
            "Run covalent AF3 prediction across the recall pool and rank by interface mPAE.",
            ["alphafold3-mpae"] if af3_enabled else [],
            "stage-three recall pool",
            af3_input_n if af3_enabled else 0,
            af3_top_n if af3_enabled else 0,
            "recall-pool identity and quota gates PASS and GPU approval",
            min(request.resource_budget.max_gpu_hours, af3_input_n * 0.25) if af3_enabled else 0,
            [
                f"Require AF3 result coverage >= {policy.min_af3_coverage:.0%}.",
                "Interface mPAE is a placement-confidence signal, not binding affinity.",
            ],
            status="planned" if af3_enabled else "blocked",
        ),
        _stage(
            6,
            "covdock-lo-gate",
            "Generate and optimize covalent poses for AF3 Top candidates, then apply A/B/C geometry gates.",
            ["schrodinger-covdock-lo"] if lo_enabled else [],
            "AF3-mPAE Top candidates",
            covdock_n if lo_enabled else 0,
            covdock_n if lo_enabled else 0,
            "AF3 coverage gate PASS, vendor license preflight PASS, and CPU approval",
            0,
            [
                f"Require CovDock success rate >= {policy.min_covdock_success_rate:.0%}.",
                "CovDock score is supporting pose evidence; AF3 rank remains primary within each gate.",
            ],
            status="planned" if lo_enabled else "skipped",
        ),
        _stage(
            7,
            "pareto-finalize",
            "Select a scaffold-diverse non-dominated set across pose gate, AF3 confidence, property fit, and chemistry risk.",
            ["library-first-pareto"] if lo_enabled and availability.pareto_selection else [],
            "CovDock A/B candidates with complete baseline evidence",
            covdock_n if lo_enabled and availability.pareto_selection else 0,
            final_n if lo_enabled and availability.pareto_selection else 0,
            "CovDock quality gates PASS and baseline Pareto evidence complete",
            0,
            ["No weighted sum is used; manual review is required before purchase."],
            status=(
                "planned"
                if lo_enabled and availability.pareto_selection
                else "skipped"
            ),
        ),
    ]

    planned_gpu = sum(stage.estimated_gpu_hours_upper for stage in stages)
    decision: Literal["READY", "CONDITIONAL", "BLOCKED", "STOP"]
    if any(stage.status == "blocked" for stage in stages):
        decision = "BLOCKED"
    elif planned_gpu > request.resource_budget.max_gpu_hours:
        decision = "CONDITIONAL"
    else:
        decision = "READY"
    return AdaptiveCascadePlan(
        request_id=request.request_id,
        project_id=request.project_id,
        decision=decision,
        library_size=library_size,
        stages=stages,
        stop_conditions=stop_conditions,
        invariants=invariants,
    )


def choose_next_action(
    plan: AdaptiveCascadePlan,
    observation: CascadeObservation,
    policy: CascadePolicy | None = None,
) -> CascadeNextAction:
    policy = policy or CascadePolicy()
    if plan.decision in {"STOP", "BLOCKED"}:
        return CascadeNextAction(
            action="stop",
            max_candidates=0,
            rationale=[f"Cascade decision is {plan.decision}."],
            requires_human_approval=False,
        )
    if not observation.explicit_failure_table:
        return CascadeNextAction(
            action="repair_inputs",
            max_candidates=observation.failed_count,
            rationale=["A complete explicit failure table is required before escalation."],
            requires_human_approval=False,
        )
    failure_fraction = (
        observation.failed_count / observation.input_count if observation.input_count else 1.0
    )
    if failure_fraction > policy.max_failure_fraction:
        return CascadeNextAction(
            action="repair_inputs",
            max_candidates=observation.failed_count,
            rationale=[
                f"Failure fraction {failure_fraction:.3f} exceeds {policy.max_failure_fraction:.3f}."
            ],
            requires_human_approval=False,
        )
    ordered = sorted(plan.stages, key=lambda stage: stage.order)
    if observation.completed_stage_id is None:
        start = 0
    else:
        completed_index = next(
            (
                index
                for index, stage in enumerate(ordered)
                if stage.stage_id == observation.completed_stage_id
            ),
            None,
        )
        if completed_index is None:
            return CascadeNextAction(
                action="review",
                max_candidates=0,
                rationale=[
                    f"Unknown completed_stage_id: {observation.completed_stage_id}."
                ],
                requires_human_approval=True,
            )
        start = completed_index + 1
    next_stage = next(
        (stage for stage in ordered[start:] if stage.status != "skipped"),
        None,
    )

    if next_stage is None or next_stage.status == "skipped":
        return CascadeNextAction(
            action="finalize",
            max_candidates=observation.success_count,
            rationale=["No further enabled stage is justified by the current evidence."],
            requires_human_approval=True,
        )
    if next_stage.status == "blocked":
        return CascadeNextAction(
            action="stop",
            stage_id=next_stage.stage_id,
            max_candidates=0,
            rationale=next_stage.rationale,
            requires_human_approval=False,
        )
    if (
        next_stage.estimated_gpu_hours_upper > 0
        and observation.remaining_gpu_hours < next_stage.estimated_gpu_hours_upper
    ):
        return CascadeNextAction(
            action="review",
            stage_id=next_stage.stage_id,
            max_candidates=min(next_stage.max_candidates, observation.success_count),
            rationale=["The declared remaining GPU budget is below the stage upper estimate."],
            requires_human_approval=True,
        )
    return CascadeNextAction(
        action="run",
        stage_id=next_stage.stage_id,
        max_candidates=min(next_stage.max_candidates, observation.success_count),
        rationale=[next_stage.trigger, *next_stage.rationale],
        requires_human_approval=next_stage.requires_human_approval,
    )
