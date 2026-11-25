from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from covscreen.research import CovalentAssessment, RoutePlan, TargetPassport, utc_now


DeNovoDecision = Literal[
    "STOP",
    "BLOCKED",
    "NEEDS_POCKET_DEFINITION",
    "NEEDS_GENERATOR_ASSETS",
    "READY_FOR_SCAFFOLD_GENERATION",
    "READY_FOR_WARHEAD_CONSTRAINT",
    "READY_FOR_MULTIOBJECTIVE_OPTIMIZATION",
    "READY_FOR_COVALENT_SCREEN",
    "READY_FOR_POOL_HANDOFF",
]


class PocketDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    structure_reference: str | None = None
    source: Literal["experimental", "p2rank", "prankweb", "other", "none"] = "none"
    center_xyz_angstrom: tuple[float, float, float] | None = None
    box_size_xyz_angstrom: tuple[float, float, float] | None = None
    pocket_residue_ids: list[str] = Field(default_factory=list)
    key_interaction_residue_ids: list[str] = Field(default_factory=list)
    hydrophobic_regions: list[str] = Field(default_factory=list)
    hydrogen_bond_donors: list[str] = Field(default_factory=list)
    hydrogen_bond_acceptors: list[str] = Field(default_factory=list)
    excluded_volume_reference: str | None = None
    warhead_reachable_region_defined: bool = False

    @model_validator(mode="after")
    def positive_box(self) -> "PocketDefinition":
        if self.box_size_xyz_angstrom and any(value <= 0 for value in self.box_size_xyz_angstrom):
            raise ValueError("all pocket box dimensions must be positive")
        return self

    def evidence_complete(self, target_site_id: str) -> bool:
        return bool(
            self.structure_reference
            and self.source != "none"
            and self.center_xyz_angstrom
            and self.box_size_xyz_angstrom
            and target_site_id in self.pocket_residue_ids
            and self.key_interaction_residue_ids
            and self.excluded_volume_reference
            and self.warhead_reachable_region_defined
        )


class DeNovoGeneratorAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pocketxmol: bool = False
    pocket2mol: bool = False
    targetdiff: bool = False
    graphbp: bool = False
    covagen: bool = False
    covalentizer_rdkit: bool = False
    rdkit: bool = False
    reinvent4: bool = False
    difflinker: bool = False
    carsidock_cov: bool = False


class DeNovoGenerationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pocket_generator: Literal["pocketxmol", "pocket2mol", "targetdiff", "graphbp"] = "pocketxmol"
    covalent_generator: Literal["covagen", "covalentizer_rdkit"] = "covagen"
    optimization_tools: list[Literal["reinvent4", "difflinker"]] = Field(
        default_factory=lambda: ["reinvent4", "difflinker"], min_length=1
    )
    max_scaffolds: int = Field(default=500, ge=1, le=10000)
    max_covalent_candidates: int = Field(default=1000, ge=1, le=20000)
    max_optimized_candidates: int = Field(default=500, ge=1, le=10000)
    require_verified_covagen_adapter: bool = True


class MultiObjectivePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pocket_fit_weight: float = Field(default=1.0, gt=0)
    pose_quality_weight: float = Field(default=1.0, gt=0)
    nac_geometry_weight: float = Field(default=1.0, gt=0)
    admet_weight: float = Field(default=0.7, gt=0)
    synthesis_weight: float = Field(default=0.8, gt=0)
    novelty_weight: float = Field(default=0.3, ge=0)
    reactivity_risk_penalty: float = Field(default=1.0, gt=0)
    max_molecular_weight: float = Field(default=650.0, gt=0)
    max_clogp: float = Field(default=6.0, gt=0)
    max_absolute_formal_charge: int = Field(default=2, ge=0, le=6)
    require_identity_and_valence_checks: bool = True
    require_structure_alert_reasons: bool = True
    require_duplicate_control: bool = True
    require_score_component_logging: bool = True


class DeNovoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    request_id: str
    project_id: str
    covalent_mode: Literal["reversible", "irreversible", "either"] = "either"
    allowed_warheads: list[str] = Field(min_length=1)
    pocket: PocketDefinition = Field(default_factory=PocketDefinition)
    generation: DeNovoGenerationPolicy = Field(default_factory=DeNovoGenerationPolicy)
    objectives: MultiObjectivePolicy = Field(default_factory=MultiObjectivePolicy)
    scaffold_candidates_path: str | None = None
    covalent_candidates_path: str | None = None
    optimized_candidates_path: str | None = None
    covalent_screen_results_path: str | None = None
    route_override_approved: bool = False
    human_approved_generation: bool = False
    human_approved_covalent_screen: bool = False

    @model_validator(mode="after")
    def ordered_artifacts(self) -> "DeNovoRequest":
        fields = [
            self.scaffold_candidates_path,
            self.covalent_candidates_path,
            self.optimized_candidates_path,
            self.covalent_screen_results_path,
        ]
        seen_missing = False
        for value in fields:
            if value is None:
                seen_missing = True
            elif seen_missing:
                raise ValueError("De novo stage artifact paths must be supplied in stage order")
        return self


class DeNovoStage(BaseModel):
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


class DeNovoPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    request_id: str
    project_id: str
    decision: DeNovoDecision
    availability: DeNovoGeneratorAvailability
    missing_assets: list[str]
    stages: list[DeNovoStage]
    invariants: list[str]
    stop_conditions: list[str]
    warnings: list[str]
    generated_at: str = Field(default_factory=utc_now)
    disclaimer: str = (
        "De novo models propose candidates; they do not establish binding, selective covalent "
        "reaction, synthetic accessibility, safety, novelty, or experimental validation."
    )


def _stage(
    order: int,
    stage_id: str,
    objective: str,
    tools: list[str],
    input_scope: str,
    outputs: list[str],
    evidence: list[str],
    cap: int,
    status: Literal["completed", "ready", "planned", "blocked", "skipped"],
    rationale: list[str],
    *,
    approval: bool = False,
) -> DeNovoStage:
    return DeNovoStage(
        order=order,
        stage_id=stage_id,
        objective=objective,
        tools=tools,
        input_scope=input_scope,
        outputs=outputs,
        completion_evidence=evidence,
        max_candidates=cap,
        status=status,
        requires_human_approval=approval,
        rationale=rationale,
    )


def plan_de_novo(
    request: DeNovoRequest,
    passport: TargetPassport,
    assessment: CovalentAssessment,
    route: RoutePlan,
    availability: DeNovoGeneratorAvailability,
    *,
    scaffold_candidates_available: bool = False,
    covalent_candidates_available: bool = False,
    optimized_candidates_available: bool = False,
    covalent_screen_results_available: bool = False,
) -> DeNovoPlan:
    site_id = passport.site.site_id
    pocket_ready = request.pocket.evidence_complete(site_id)
    route_blocked = route.selected_route != "de-novo" and not request.route_override_approved
    site_stopped = assessment.decision == "STOP" or not passport.site.nucleophile_present

    # Completion is cumulative: a later artifact never repairs a missing parent.
    scaffold_ready = scaffold_candidates_available
    covalent_ready = scaffold_ready and covalent_candidates_available
    optimized_ready = covalent_ready and optimized_candidates_available
    screened = optimized_ready and covalent_screen_results_available

    needed = [request.generation.pocket_generator, request.generation.covalent_generator]
    needed.extend(request.generation.optimization_tools)
    needed.extend(["rdkit", "carsidock_cov"])
    missing_assets = sorted({name for name in needed if not getattr(availability, name)})

    if site_stopped:
        decision: DeNovoDecision = "STOP"
    elif route_blocked:
        decision = "BLOCKED"
    elif not pocket_ready:
        decision = "NEEDS_POCKET_DEFINITION"
    elif missing_assets:
        decision = "NEEDS_GENERATOR_ASSETS"
    elif not scaffold_ready:
        decision = "READY_FOR_SCAFFOLD_GENERATION"
    elif not covalent_ready:
        decision = "READY_FOR_WARHEAD_CONSTRAINT"
    elif not optimized_ready:
        decision = "READY_FOR_MULTIOBJECTIVE_OPTIMIZATION"
    elif not screened:
        decision = "READY_FOR_COVALENT_SCREEN"
    else:
        decision = "READY_FOR_POOL_HANDOFF"

    hard_block = decision in {"STOP", "BLOCKED"}
    asset_block = decision == "NEEDS_GENERATOR_ASSETS"
    generation_block = hard_block or not pocket_ready or asset_block
    stages = [
        _stage(0, "project-and-route-gate", "Verify target, residue, mechanism, and approved De novo route.",
               ["covalent_validate_project"], "project dossier", ["validated target/site/route"],
               ["GO or CONDITIONAL_GO", "approved De novo route or recorded override"], 1,
               "blocked" if hard_block else "completed", ["A route decision authorizes planning only."]),
        _stage(1, "pocket-definition", "Define the 3D generation region, interaction features, excluded volume, and warhead-reachable region.",
               ["P2Rank", "PrankWeb"], "validated target structure", ["pocket box", "residue map", "feature map", "excluded volume", "reachable region"],
               ["target residue is inside the pocket", "coordinates and provenance are traceable"], 1,
               "blocked" if hard_block else "completed" if pocket_ready else "ready",
               ["Pocket prediction is a hypothesis and must be checked against experimental structure evidence."]),
        _stage(2, "pocket-conditioned-3d-scaffold-generation", "Generate diverse 3D recognition scaffolds conditioned on the pocket.",
               [request.generation.pocket_generator], "audited pocket definition", ["3D scaffold candidates", "generator provenance", "sampling seeds", "failure table"],
               ["valid identity and valence", "pocket-conditioned coordinates", "diversity and duplicate report"], request.generation.max_scaffolds,
               "blocked" if generation_block else "completed" if scaffold_ready else "ready",
               ["PocketXMol/Pocket2Mol/TargetDiff/GraphBP propose scaffolds; no generated score is treated as binding evidence."], approval=True),
        _stage(3, "target-residue-warhead-constrained-generation", "Attach or generate an allowed warhead under target-residue and 3D reachability constraints.",
               [request.generation.covalent_generator, "RDKit SMARTS"], "validated 3D scaffolds", ["covalent candidates", "warhead mapping", "reaction atom mapping", "rejected structures and reasons"],
               ["allowed warhead SMARTS match", "single explicit reaction atom mapping", "target residue compatibility", "reachable geometry"], request.generation.max_covalent_candidates,
               "blocked" if generation_block or not scaffold_ready else "completed" if covalent_ready else "ready",
               [
                   "CovaGEN requires a verified adapter and model provenance; RDKit validates chemistry but does not prove selectivity."
                   if request.generation.covalent_generator == "covagen"
                   else "covalentizer-rdkit is a deterministic attachment enumerator with explicit SMARTS, atom indices, and valence checks; it is not a learned CovaGEN substitute."
               ], approval=True),
        _stage(4, "linker-rgroup-multiobjective-optimization", "Optimize linker and R-groups across pocket, pose, NAC, ADMET, synthesis, novelty, and reactivity risk.",
               request.generation.optimization_tools, "warhead-constrained candidates", ["optimized candidates", "all score components", "Pareto fronts", "filter and failure table"],
               ["every reward component logged", "hard descriptor limits enforced", "duplicates and alerts retained with reasons", "no single-score winner"], request.generation.max_optimized_candidates,
               "blocked" if generation_block or not covalent_ready else "completed" if optimized_ready else "ready",
               ["Anti-reward-hacking gates reject oversized, overly lipophilic, highly charged, overreactive, or implausibly synthesizable designs."], approval=True),
        _stage(5, "carsidock-cov-pose-screen", "Generate and rank covalent poses and evaluate near-attack geometry.",
               ["CarsiDock-Cov"], "optimized covalent candidates", ["covalent poses", "CarsiScore", "reaction geometry", "screen failures"],
               ["pose provenance", "reaction atom constraints", "distance/angle/clash checks", "full denominator retained"], request.generation.max_optimized_candidates,
               "blocked" if generation_block or not optimized_ready else "completed" if screened else "ready",
               ["CarsiDock-Cov is a screen, not proof of reaction or affinity."], approval=True),
        _stage(6, "candidate-pool-handoff", "Merge qualified designs into the unified candidate pool for independent pose consensus and downstream triage.",
               ["candidate-pool registry"], "screened De novo candidates", ["standardized candidates", "route provenance", "parent/nonreactive controls", "uncertainty fields"],
               ["identity deduplication", "route and model provenance", "all scores retain direction and uncertainty"], request.generation.max_optimized_candidates,
               "blocked" if generation_block or not screened else "ready",
               ["Downstream CarsiDock/AF3 consensus, QM, ADMET, synthesis, MD/QM-MM, and experiments remain separate gates."])
    ]

    invariants = [
        "Use one primary pocket generator and one verified covalent generator in V1; alternatives are fallbacks, not an ensemble claim.",
        "The target nucleophile, reaction atom, allowed warhead, and 3D reachability remain explicit at every stage.",
        "Keep generated, rejected, failed, filtered, and duplicate records with model versions, seeds, and reasons.",
        "Medicinal-chemistry alerts and property filters are context-specific flags, not binary truth labels.",
        "A generator proposes candidates; CarsiDock-Cov screens poses; neither is the final scientific judge.",
        "Require human approval before generation, optimization, docking, or any expensive execution.",
    ]
    stop_conditions = [
        "Target construct, residue identity, or nucleophile mapping fails validation.",
        "The target residue is outside the defined pocket or no warhead-reachable region exists.",
        "Generator code, checkpoint, license, or adapter provenance cannot be verified.",
        "Candidates fail identity, valence, reaction mapping, geometry, or traceability checks.",
        "Optimization exploits one reward through size, lipophilicity, charge, nonspecific reactivity, or synthetic implausibility.",
    ]
    warnings = []
    if missing_assets:
        warnings.append("Unavailable required assets: " + ", ".join(missing_assets))
    if request.route_override_approved and route.selected_route != "de-novo":
        warnings.append(f"De novo route override recorded; project route is {route.selected_route}.")
    return DeNovoPlan(
        request_id=request.request_id,
        project_id=request.project_id,
        decision=decision,
        availability=availability,
        missing_assets=missing_assets,
        stages=stages,
        invariants=invariants,
        stop_conditions=stop_conditions,
        warnings=warnings,
    )
