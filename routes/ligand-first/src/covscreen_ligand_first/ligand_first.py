from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from covscreen.attachment import AttachmentPlanResult, AttachmentRequest, plan_attachments
from covscreen.research import CovalentAssessment, RoutePlan, TargetPassport, utc_now


LigandFirstDecision = Literal[
    "STOP",
    "BLOCKED",
    "NEEDS_NONCOVALENT_POSE",
    "NEEDS_SCAFFOLD_ANNOTATION",
    "NEEDS_ATTACHMENT_EVIDENCE",
    "READY_FOR_GENERATION",
    "READY_FOR_COVALENT_SCREEN",
    "READY_FOR_POOL_HANDOFF",
]


class LigandFirstGenerationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_tools: list[Literal["covalentizer", "rdkit"]] = Field(
        default_factory=lambda: ["covalentizer", "rdkit"]
    )
    optional_generation_tools: list[
        Literal["difflinker", "reinvent4", "delinker", "fragmenstein", "buildamol"]
    ] = Field(default_factory=lambda: ["difflinker", "reinvent4"])
    allow_generative_models: bool = False
    max_generated_per_attachment: int = Field(default=50, ge=1, le=1000)
    max_total_candidates: int = Field(default=500, ge=1, le=10000)


class CovalentGeometryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_scaffold_core_rmsd_angstrom: float = Field(default=2.0, gt=0)
    min_parent_interaction_retention: float = Field(default=0.70, ge=0, le=1)
    reaction_distance_window_angstrom: tuple[float, float] = (1.7, 4.5)
    attack_angle_window_degrees: tuple[float, float] = (80.0, 140.0)
    max_steric_clash_fraction: float = Field(default=0.20, ge=0, le=1)
    require_linker_strain_assessment: bool = True
    require_pose_ensemble_consistency: bool = True

    @model_validator(mode="after")
    def ordered_windows(self) -> "CovalentGeometryPolicy":
        if self.reaction_distance_window_angstrom[0] >= self.reaction_distance_window_angstrom[1]:
            raise ValueError("reaction distance window must be increasing")
        if self.attack_angle_window_degrees[0] >= self.attack_angle_window_degrees[1]:
            raise ValueError("attack angle window must be increasing")
        return self


class LigandFirstRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    request_id: str
    project_id: str
    attachment: AttachmentRequest
    generation: LigandFirstGenerationPolicy = Field(default_factory=LigandFirstGenerationPolicy)
    geometry_screen: CovalentGeometryPolicy = Field(default_factory=CovalentGeometryPolicy)
    generated_candidates_path: str | None = None
    covalent_screen_results_path: str | None = None
    human_approved_generation: bool = False
    human_approved_covalent_screen: bool = False

    @model_validator(mode="after")
    def consistent_project(self) -> "LigandFirstRequest":
        if self.attachment.project_id != self.project_id:
            raise ValueError("attachment.project_id must match project_id")
        if self.covalent_screen_results_path and not self.generated_candidates_path:
            raise ValueError("covalent screen results require generated_candidates_path")
        return self


class LigandFirstStage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: int = Field(ge=0)
    stage_id: str
    objective: str
    tools: list[str]
    input_scope: str
    outputs: list[str]
    completion_evidence: list[str]
    max_candidates: int = Field(ge=0)
    status: Literal["completed", "ready", "planned", "blocked", "skipped"]
    requires_human_approval: bool
    rationale: list[str]


class LigandFirstPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    request_id: str
    project_id: str
    ligand_id: str
    decision: LigandFirstDecision
    attachment_plan: AttachmentPlanResult
    eligible_attachment_ranks: list[int]
    stages: list[LigandFirstStage]
    invariants: list[str]
    stop_conditions: list[str]
    generated_at: str = Field(default_factory=utc_now)
    disclaimer: str = (
        "This plan operationalizes Ligand-first Covalentization from a supported parent-ligand "
        "pose. It does not claim that enumerated molecules exist, are synthetically accessible, "
        "bind, react selectively, or have been experimentally validated."
    )


def _stage(
    order: int,
    stage_id: str,
    objective: str,
    tools: list[str],
    input_scope: str,
    outputs: list[str],
    completion_evidence: list[str],
    max_candidates: int,
    status: Literal["completed", "ready", "planned", "blocked", "skipped"],
    rationale: list[str],
    *,
    approval: bool = False,
) -> LigandFirstStage:
    return LigandFirstStage(
        order=order,
        stage_id=stage_id,
        objective=objective,
        tools=tools,
        input_scope=input_scope,
        outputs=outputs,
        completion_evidence=completion_evidence,
        max_candidates=max_candidates,
        status=status,
        requires_human_approval=approval,
        rationale=rationale,
    )


def plan_ligand_first(
    request: LigandFirstRequest,
    passport: TargetPassport,
    assessment: CovalentAssessment,
    route: RoutePlan,
    *,
    generated_candidates_available: bool = False,
    covalent_screen_results_available: bool = False,
) -> LigandFirstPlan:
    attachment = plan_attachments(request.attachment, passport, assessment, route)
    invariants = [
        "Start from a traceable parent ligand and a supported noncovalent binding pose.",
        "Protect the scaffold core and quantified parent interactions before covalentization.",
        "Jointly plan attachment point, 3D vector, linker necessity, and warhead compatibility.",
        "Treat Covalentizer as a rule baseline and generative models as proposal tools, not judges.",
        "Require CarsiDock-Cov geometry screening before candidate-pool handoff.",
        "Retain rejected designs, filter reasons, uncertainties, and the parent/nonreactive controls.",
    ]
    stop_conditions = [
        "The target construct or reactive residue no longer matches the Target Passport.",
        "No supported parent-ligand pose can be obtained.",
        "All modifiable atoms disrupt protected pharmacophore interactions or point away from the site.",
        "No warhead is compatible with the residue, covalent mode, geometry, and reactivity policy.",
        "Generated structures fail identity, valence, geometry, or traceability checks.",
    ]
    route_blocked = route.selected_route != "ligand-first" and not request.attachment.route_override_approved
    site_stopped = assessment.decision == "STOP" or not passport.site.nucleophile_present
    pose_ready = (
        request.attachment.pose_source != "none"
        and request.attachment.pose_reference is not None
    )
    scaffold_ready = bool(request.attachment.scaffold_core_atom_indices) and bool(
        request.attachment.key_interaction_atom_indices
    )
    eligible = [candidate.rank for candidate in attachment.candidates if candidate.evidence_complete]
    attachment_ready = attachment.status == "READY" and bool(eligible)

    generation_tools = list(request.generation.baseline_tools)
    if request.generation.allow_generative_models:
        generation_tools.extend(request.generation.optional_generation_tools)
    generation_cap = min(
        request.generation.max_total_candidates,
        len(eligible) * request.generation.max_generated_per_attachment,
    )

    if site_stopped:
        decision: LigandFirstDecision = "STOP"
    elif route_blocked:
        decision = "BLOCKED"
    elif not pose_ready:
        decision = "NEEDS_NONCOVALENT_POSE"
    elif not scaffold_ready:
        decision = "NEEDS_SCAFFOLD_ANNOTATION"
    elif not attachment_ready:
        decision = "NEEDS_ATTACHMENT_EVIDENCE"
    elif not generated_candidates_available:
        decision = "READY_FOR_GENERATION"
    elif not covalent_screen_results_available:
        decision = "READY_FOR_COVALENT_SCREEN"
    else:
        decision = "READY_FOR_POOL_HANDOFF"

    gated = decision in {"STOP", "BLOCKED"}
    stages = [
        _stage(
            0,
            "project-and-route-gate",
            "Verify Target Passport, covalent-site evidence, mechanism, and approved Ligand-first route.",
            ["covalent_validate_project"],
            "project dossier",
            ["validated target/site/route decision"],
            ["GO or CONDITIONAL_GO assessment", "approved Ligand-first route"],
            1,
            "blocked" if gated else "completed",
            ["A route GO permits planning only; it is not evidence that a conjugate will work."],
        ),
        _stage(
            1,
            "noncovalent-pose-acquisition",
            "Obtain the parent ligand and a traceable noncovalent binding pose.",
            ["PDB", "CarsiDock", "KarmaDock", "AutoDock Vina", "Glide", "AlphaFold 3"],
            "known noncovalent ligand or hit",
            ["parent ligand identity", "pose reference", "pose provenance", "pose confidence"],
            ["experimental complex preferred", "otherwise an independently reviewable docked pose"],
            1,
            "blocked" if gated else "completed" if pose_ready else "ready",
            ["AlphaFold 3 is an independent structural reference, not a reaction or affinity oracle."],
        ),
        _stage(
            2,
            "scaffold-modification-map",
            "Define the retained scaffold, key interactions, protected pharmacophore, and modifiable atoms.",
            ["RDKit", "Covalentizer"],
            "parent ligand plus noncovalent pose",
            ["scaffold core atoms", "key interaction atoms", "protected atoms", "modifiable atom map"],
            ["aromaticity and valence preserved", "critical interactions explicitly protected"],
            len(request.attachment.attachment_atom_indices),
            "blocked" if gated or not pose_ready else "completed" if scaffold_ready else "ready",
            ["Covalentizer is a rules baseline; modification decisions remain evidence-traceable."],
        ),
        _stage(
            3,
            "attachment-vector-linker-warhead-plan",
            "Jointly rank attachment points, 3D vectors, linker necessity, and warhead compatibility.",
            ["Covalentizer", "Attachment Planner"],
            "modifiable scaffold atoms with atom-level pose evidence",
            ["ranked attachment hypotheses", "vector scores", "linker classes", "warhead hypotheses"],
            [
                "distance and vector angle",
                "steric clash fraction",
                "parent-interaction retention",
                "attachment-site synthetic accessibility",
            ],
            len(attachment.candidates),
            "blocked" if gated or not pose_ready or not scaffold_ready else "completed" if attachment_ready else "ready",
            ["Drug-likeness and alert filters are context-dependent flags, never proof of quality."],
        ),
        _stage(
            4,
            "warhead-linker-generation",
            "Enumerate rule baselines and, when approved, propose 3D linkers and optimized R-groups.",
            generation_tools,
            "evidence-complete attachment hypotheses only",
            ["traceable candidate structures", "parent/attachment/linker/warhead lineage", "generation failures"],
            ["RDKit valence and identity checks", "deduplication", "warhead SMARTS verification"],
            generation_cap,
            "blocked"
            if not attachment_ready
            else "completed"
            if generated_candidates_available
            else "ready"
            if request.human_approved_generation
            else "planned",
            [
                "Covalentizer and RDKit are the V1 baseline.",
                "DiffLinker/REINVENT4 proposals require explicit approval and downstream screening.",
            ],
            approval=True,
        ),
        _stage(
            5,
            "covalent-pose-geometry-screen",
            "Refine covalent poses and reject candidates that cannot preserve recognition and reaction geometry.",
            ["CarsiDock-Cov"],
            "validated generated candidates",
            ["covalent poses", "geometry table", "explicit failure table", "screen provenance"],
            [
                "scaffold core RMSD",
                "parent-interaction retention",
                "warhead distance and attack angle",
                "linker and ligand strain",
                "protein clash and pose-ensemble consistency",
            ],
            generation_cap,
            "blocked"
            if not generated_candidates_available
            else "completed"
            if covalent_screen_results_available
            else "ready"
            if request.human_approved_covalent_screen
            else "planned",
            ["CarsiDock-Cov is a structural screen; it does not establish kinact or selectivity."],
            approval=True,
        ),
        _stage(
            6,
            "candidate-pool-handoff",
            "Standardize, deduplicate, cluster, and hand traceable Ligand-first candidates to common evaluation.",
            ["RDKit", "PostgreSQL"],
            "geometry-screened candidates and failures",
            ["Candidate Passports", "deduplicated pool", "diversity clusters", "failure ledger"],
            ["route and parent lineage", "all scores with direction", "uncertainty and failure flags"],
            generation_cap if covalent_screen_results_available else 0,
            "ready" if covalent_screen_results_available else "blocked",
            [
                "Handoff leads to pose consensus, reactivity/QM, ADMET, synthesis, MD, and experiments; "
                "none are implied by this plan."
            ],
            approval=True,
        ),
    ]
    return LigandFirstPlan(
        request_id=request.request_id,
        project_id=request.project_id,
        ligand_id=request.attachment.ligand_id,
        decision=decision,
        attachment_plan=attachment,
        eligible_attachment_ranks=eligible,
        stages=stages,
        invariants=invariants,
        stop_conditions=stop_conditions,
    )
