from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EvidenceStatus = Literal["supported", "challenged", "unknown"]
Decision = Literal["GO", "CONDITIONAL_GO", "STOP"]
Route = Literal["library-first", "ligand-first", "de-novo", "evidence-first"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source_type: Literal["uniprot", "pdb", "publication", "database", "other"]
    title: str
    url: str
    retrieved_on: date
    doi: str | None = None
    pmid: str | None = None
    supports: list[str] = Field(min_length=1)


class ReactiveSite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_id: str
    chain_id: str
    residue_name: str
    residue_number: int = Field(gt=0)
    reactive_atom: str | None = None
    nucleophile_present: bool
    reaction_hypothesis: str
    sequence_context: str | None = None


class TargetPassport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    target_id: str
    symbol: str
    name: str
    organism: str
    uniprot_id: str
    disease_context: str
    target_hypothesis: str
    site: ReactiveSite
    structures: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(min_length=2)
    known_unknowns: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_ids_are_unique(self) -> "TargetPassport":
        ids = [item.id for item in self.citations]
        if len(ids) != len(set(ids)):
            raise ValueError("citation ids must be unique")
        return self


class ResearchProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    project_id: str
    title: str
    objective: str
    status: Literal["draft", "evidence_ready", "planned", "running", "closed"] = "draft"
    target_passport: str = "target_passport.json"
    created_at: str = Field(default_factory=utc_now)
    benchmark_links: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_project_id(self) -> "ResearchProject":
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", self.project_id):
            raise ValueError("project_id must be a 3–64 character lowercase slug")
        return self


class AssessmentCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Literal[
        "biological_relevance",
        "site_identity",
        "structural_access",
        "ligandability",
        "covalent_precedent",
        "reaction_plausibility",
        "selectivity_window",
    ]
    status: EvidenceStatus
    weight: float = Field(gt=0, le=5)
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)


class CovalentAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    project_id: str
    decision: Decision
    evidence_coverage: float = Field(ge=0, le=1)
    support_score: float = Field(ge=-1, le=1)
    criteria: list[AssessmentCriterion]
    blockers: list[str]
    uncertainties: list[str]
    generated_at: str = Field(default_factory=utc_now)
    method: str = "deterministic-evidence-matrix-v1"
    disclaimer: str = (
        "This is an evidence-triage decision, not a prediction of clinical efficacy "
        "or experimental validation."
    )


class HypothesisCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    id: str
    project_id: str
    statement: str
    status: Literal["proposed", "testing", "supported", "contradicted", "retired"]
    confidence: float = Field(ge=0, le=1)
    assumptions: list[str]
    supporting_evidence: list[str]
    disconfirming_evidence: list[str]
    next_test: str
    human_decision: str | None = None


class AssetInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    screening_library_available: bool = False
    screening_library_path: str | None = None
    known_ligands: bool = False
    reusable_scaffold: bool = False
    prepared_receptor: bool = False
    reference_structure: bool = False
    generative_model_available: bool = False
    notes: list[str] = Field(default_factory=list)


class RoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    project_id: str
    selected_route: Route
    decision: Decision
    rationale: list[str]
    prerequisites: list[str]
    alternatives: list[str]
    stop_conditions: list[str]
    human_approval_required: bool = True
    generated_at: str = Field(default_factory=utc_now)


class ResourceBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_gpu_hours: float = Field(ge=0)
    max_wall_hours: float = Field(gt=0)
    max_concurrent_jobs: int = Field(gt=0)


class EngineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: Literal[
        "carsidock-cov",
        "carsidock-cov-rtmscore",
        "alphafold3-mpae",
        "schrodinger-covdock-vs",
        "schrodinger-covdock-lo",
    ]
    enabled: bool = True
    parameters: dict[str, Any] = Field(default_factory=dict)


class LibraryFirstRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    request_id: str
    project_id: str
    target_passport_path: str
    receptor_path: str
    ligand_table_path: str
    compound_id_column: str = "compound_id"
    smiles_column: str = "standardized_smiles"
    site_id: str
    chain_id: str
    residue_name: str
    residue_number: int = Field(gt=0)
    reactive_atom: str
    reaction_type: str
    output_dir: str
    engines: list[EngineRequest] = Field(min_length=1)
    resource_budget: ResourceBudget
    dry_run: bool = True


class LibraryFirstResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    request_id: str
    engine: str
    input_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    status: Literal["SUCCESS", "PARTIAL", "FAILED", "UNAVAILABLE"]
    score_direction: Literal["higher_is_better", "lower_is_better"]
    result_table: str | None = None
    failure_table: str | None = None
    provenance: dict[str, Any]

    @model_validator(mode="after")
    def counts_balance(self) -> "LibraryFirstResult":
        if self.success_count + self.failed_count != self.input_count:
            raise ValueError("success_count + failed_count must equal input_count")
        return self


class ProjectSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: ResearchProject
    target_passport: TargetPassport
    assessment_criteria: list[AssessmentCriterion] = Field(min_length=7, max_length=7)
    assets: AssetInventory
    search_protocol: "EvidenceSearchProtocol"


class EvidenceSearchProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    searched_on: date
    databases: list[str] = Field(min_length=3)
    queries: list[str] = Field(min_length=2)
    inclusion_rule: str
    exclusion_rule: str
    limitations: list[str] = Field(min_length=1)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: BaseModel | dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, list):
        payload = [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            for item in value
        ]
    else:
        payload = value
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def assess_covalent_project(
    project_id: str,
    passport: TargetPassport,
    criteria: list[AssessmentCriterion],
) -> CovalentAssessment:
    expected = {
        "biological_relevance",
        "site_identity",
        "structural_access",
        "ligandability",
        "covalent_precedent",
        "reaction_plausibility",
        "selectivity_window",
    }
    found = {item.id for item in criteria}
    if found != expected or len(criteria) != len(expected):
        raise ValueError(f"assessment requires exactly these criteria: {sorted(expected)}")
    citation_ids = {item.id for item in passport.citations}
    unknown_ids = sorted(
        {
            evidence_id
            for item in criteria
            for evidence_id in item.evidence_ids
            if evidence_id not in citation_ids
        }
    )
    if unknown_ids:
        raise ValueError(f"unknown evidence ids in assessment: {unknown_ids}")

    total_weight = sum(item.weight for item in criteria)
    known_weight = sum(item.weight for item in criteria if item.status != "unknown")
    signed = {"supported": 1.0, "challenged": -1.0, "unknown": 0.0}
    support_score = sum(item.weight * signed[item.status] for item in criteria) / total_weight
    blockers: list[str] = []
    by_id = {item.id: item for item in criteria}
    if not passport.site.nucleophile_present:
        blockers.append("The nominated residue is not the claimed nucleophile in the target context.")
    for critical in ("biological_relevance", "site_identity", "structural_access"):
        if by_id[critical].status == "challenged":
            blockers.append(f"Critical criterion challenged: {critical}.")
    supported_weight = sum(item.weight for item in criteria if item.status == "supported")
    if blockers:
        decision: Decision = "STOP"
    elif supported_weight / total_weight >= 0.7 and known_weight / total_weight >= 0.8:
        decision = "GO"
    else:
        decision = "CONDITIONAL_GO"
    return CovalentAssessment(
        project_id=project_id,
        decision=decision,
        evidence_coverage=round(known_weight / total_weight, 4),
        support_score=round(support_score, 4),
        criteria=criteria,
        blockers=blockers,
        uncertainties=[item.rationale for item in criteria if item.status == "unknown"],
    )


def make_hypothesis_card(
    project_id: str,
    passport: TargetPassport,
    assessment: CovalentAssessment,
) -> HypothesisCard:
    evidence = [
        evidence_id
        for item in assessment.criteria
        if item.status == "supported"
        for evidence_id in item.evidence_ids
    ]
    confidence = max(0.0, min(1.0, (assessment.support_score + 1) / 2))
    if assessment.decision == "STOP":
        statement = (
            f"The nominated {passport.symbol} {passport.site.site_id} covalent strategy "
            "is not supported in the stated molecular context."
        )
        next_test = "Correct the target sequence/site context before any compound calculation."
    else:
        statement = (
            f"A ligand that first forms selective reversible interactions with {passport.symbol} "
            f"may position a controlled electrophile for {passport.site.site_id} engagement."
        )
        next_test = (
            "Measure reversible affinity, time-dependent inhibition, intact-protein adduct "
            "formation, and a site-mutant control before claiming targeted covalency."
        )
    return HypothesisCard(
        id=f"hyp-{project_id}-001",
        project_id=project_id,
        statement=statement,
        status="proposed",
        confidence=round(confidence, 4),
        assumptions=[
            "Structural observations transfer to the intended construct and assay state.",
            "Warhead reactivity can be tuned without making nonspecific reactivity dominant.",
        ],
        supporting_evidence=sorted(set(evidence)),
        disconfirming_evidence=[
            "Loss of activity cannot be rescued by reversible recognition.",
            "No site-specific adduct by intact or peptide-level mass spectrometry.",
            "Comparable labeling of unrelated proteins or the site-mutant control.",
        ],
        next_test=next_test,
    )


def plan_route(
    project_id: str,
    assessment: CovalentAssessment,
    assets: AssetInventory,
) -> RoutePlan:
    if assessment.decision == "STOP":
        return RoutePlan(
            project_id=project_id,
            selected_route="evidence-first",
            decision="STOP",
            rationale=assessment.blockers,
            prerequisites=["Resolve the target/site identity and repeat the evidence assessment."],
            alternatives=[],
            stop_conditions=["Do not launch docking or generation while a critical blocker remains."],
        )
    if assets.screening_library_available and assets.prepared_receptor:
        route: Route = "library-first"
        rationale = ["A screening library and a prepared receptor are both available."]
        prerequisites = ["Validate compound identities and the covalent reaction mapping."]
    elif assets.known_ligands or assets.reusable_scaffold:
        route = "ligand-first"
        rationale = ["Known binders or a reusable recognition scaffold provide a grounded starting point."]
        prerequisites = ["Separate reversible recognition from warhead placement and intrinsic reactivity."]
    elif assets.reference_structure and assets.generative_model_available:
        route = "de-novo"
        rationale = ["A reference structure and a generative route are available, but no reusable ligand assets are declared."]
        prerequisites = ["Apply synthesis, reactivity, selectivity, and novelty gates before computation."]
    else:
        route = "evidence-first"
        rationale = ["Declared assets are insufficient for a defensible design or screening route."]
        prerequisites = ["Acquire a validated structure and either a ligand/scaffold or a curated screening library."]
    return RoutePlan(
        project_id=project_id,
        selected_route=route,
        decision=assessment.decision,
        rationale=rationale,
        prerequisites=prerequisites,
        alternatives=[
            item
            for item in ("library-first", "ligand-first", "de-novo")
            if item != route
        ],
        stop_conditions=[
            "Stop if target/site identity fails sequence or structure validation.",
            "Stop if controls indicate nonspecific covalent reactivity.",
            "Do not equate a computational score with experimental target engagement.",
        ],
    )


def create_project(seed: ProjectSeed, projects_root: Path) -> Path:
    project_dir = projects_root / seed.project.project_id
    if project_dir.exists():
        raise FileExistsError(f"project already exists: {project_dir}")
    for subdir in (
        "assessment",
        "evidence",
        "hypotheses",
        "plans",
        "library",
        "runs",
        "state",
        "structures",
    ):
        (project_dir / subdir).mkdir(parents=True, exist_ok=True)
    assessment = assess_covalent_project(
        seed.project.project_id, seed.target_passport, seed.assessment_criteria
    )
    hypothesis = make_hypothesis_card(seed.project.project_id, seed.target_passport, assessment)
    route = plan_route(seed.project.project_id, assessment, seed.assets)
    write_json(project_dir / "project.json", seed.project)
    write_json(project_dir / "target_passport.json", seed.target_passport)
    write_json(project_dir / "evidence" / "evidence_register.json", seed.target_passport.citations)
    write_json(project_dir / "evidence" / "search_protocol.json", seed.search_protocol)
    write_json(project_dir / "assessment" / "covalent_assessment.json", assessment)
    write_json(project_dir / "hypotheses" / "hypothesis-001.json", hypothesis)
    write_json(project_dir / "plans" / "route_plan.json", route)
    write_json(project_dir / "plans" / "asset_inventory.json", seed.assets)
    return project_dir


def validate_project(project_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        project = ResearchProject.model_validate(read_json(project_dir / "project.json"))
        passport = TargetPassport.model_validate(read_json(project_dir / project.target_passport))
        EvidenceSearchProtocol.model_validate(
            read_json(project_dir / "evidence" / "search_protocol.json")
        )
        assessment = CovalentAssessment.model_validate(
            read_json(project_dir / "assessment" / "covalent_assessment.json")
        )
        HypothesisCard.model_validate(
            read_json(project_dir / "hypotheses" / "hypothesis-001.json")
        )
        route = RoutePlan.model_validate(read_json(project_dir / "plans" / "route_plan.json"))
        AssetInventory.model_validate(
            read_json(project_dir / "plans" / "asset_inventory.json")
        )
        if project.project_id != assessment.project_id or project.project_id != route.project_id:
            errors.append("project_id mismatch across artifacts")
        source_ids = {item.id for item in passport.citations}
        used_ids = {
            evidence_id
            for item in assessment.criteria
            for evidence_id in item.evidence_ids
        }
        if not used_ids.issubset(source_ids):
            errors.append("assessment references missing evidence")
    except (OSError, ValueError, KeyError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return {
        "project_dir": str(project_dir),
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }


def validate_library_first_request(
    request: LibraryFirstRequest,
    project_dir: Path,
    require_inputs: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    if request.project_id != project_dir.name:
        errors.append("request project_id does not match project directory")
    try:
        passport = TargetPassport.model_validate(
            read_json(project_dir / request.target_passport_path)
        )
        site = passport.site
        request_site = (
            request.site_id,
            request.chain_id,
            request.residue_name,
            request.residue_number,
            request.reactive_atom,
        )
        passport_site = (
            site.site_id,
            site.chain_id,
            site.residue_name,
            site.residue_number,
            site.reactive_atom,
        )
        if request_site != passport_site:
            errors.append("request reactive site does not exactly match Target Passport")
    except (OSError, ValueError, KeyError) as exc:
        errors.append(f"Target Passport validation failed: {exc}")
    if require_inputs:
        for label, relative in (
            ("receptor", request.receptor_path),
            ("ligand table", request.ligand_table_path),
        ):
            if not (project_dir / relative).is_file():
                errors.append(f"{label} input missing: {relative}")
    enabled = [item for item in request.engines if item.enabled]
    if not enabled:
        errors.append("at least one engine must be enabled")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "project": ResearchProject,
    "target-passport": TargetPassport,
    "covalent-assessment": CovalentAssessment,
    "hypothesis-card-v2": HypothesisCard,
    "route-plan": RoutePlan,
    "library-first-request": LibraryFirstRequest,
    "library-first-result": LibraryFirstResult,
}


def write_schemas(schema_dir: Path) -> list[str]:
    outputs = []
    for name, model in SCHEMA_MODELS.items():
        path = schema_dir / f"{name}.schema.json"
        write_json(path, model.model_json_schema())
        outputs.append(str(path))
    return outputs
