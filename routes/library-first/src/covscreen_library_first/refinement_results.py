"""Admit a fresh native report without reimplementing scientific classifiers."""
from copy import deepcopy
from typing import Annotated, Literal
from pydantic import Field
from covscreen.workflow_protocol import StrictModel, changed
from covscreen.refinement_quality import STRICT_POLICY, validate_policy
from .screening_results import Candidate as SelectedCandidate
from .refinement_execution import Observation

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Count = Annotated[int, Field(ge=0, le=100)]
Number = Annotated[float, Field(allow_inf_nan=False)]
Status = Literal["BOTH_STRUCTURAL_QC_PASSED", "STRUCTURAL_QC_DISAGREEMENT", "BOTH_STRUCTURAL_QC_REJECTED", "NOT_FULLY_EVALUATED"]


class AF3(StrictModel):
    engine: Literal["af3"]
    mpae: float = Field(ge=0, allow_inf_nan=False)
    iptm: float = Field(ge=0, le=1, allow_inf_nan=False)
    ligand_plddt_mean: float = Field(ge=0, le=100, allow_inf_nan=False)
    has_clash: bool


class StrainProvenance(StrictModel):
    source_field: Literal["r_strain_Local_Strain_Energy"]
    unit: Literal["kcal/mol"]
    definition: Literal["vendor_local_strain_unmodified"]
    status: Literal["VALID", "MISSING", "INVALID_OR_NONFINITE"]


class Assessment(StrictModel):
    version: Literal["refinement-independent-axes-v1"]
    physical_validity: Literal["PASS", "FAIL", "UNKNOWN", "NOT_EVALUATED"]
    evidence_complete: bool
    review_eligible: bool
    missing_reasons: list[str]
    failure_reasons: list[str]
    calibration_status: Literal["provisional"]


class CovDock(StrictModel):
    engine: Literal["covdock"]
    covalent_link_present: bool | None
    geometry_inspection_status: str | None
    target_atom_match_count: Number | None
    external_target_bond_count: Number | None
    ligand_heavy_atom_count: Number | None
    covalent_bond_length_angstrom: Number | None
    minimum_nonbonded_heavy_atom_distance_angstrom: Number | None
    severe_clash: bool | None
    strain_kcal_mol: Number | None
    strain_provenance: StrainProvenance | None = None


class Quality(StrictModel):
    status: Literal["PASSED", "REJECTED", "NOT_EVALUATED"]
    reason: str | None = Field(min_length=1, max_length=2000)
    assessment: Assessment | None = None


class Engine(StrictModel):
    status: Literal["SUCCESS", "FAILED"]
    score: Number | None
    quality: Quality
    measurements: AF3 | CovDock | None


class Candidate(SelectedCandidate):
    status: Status
    af3: Engine
    covdock: Engine


class Stage(StrictModel):
    engine: Literal["af3", "covdock"]
    task_id: str = Field(pattern=r"^refine-[a-f0-9]{24}$")
    plan_digest: Hash
    outcome_digest: Hash
    output_manifest_sha256: Hash
    success_count: Count
    failed_count: Count


class Report(StrictModel):
    report_digest: Hash
    quality_policy_digest: Hash
    input_identity_sha256: Hash
    candidate_count: int = Field(ge=1, le=100)
    candidates: list[Candidate] = Field(min_length=1, max_length=100)
    counts: dict[Status, Count]
    stages: list[Stage] = Field(min_length=2, max_length=2)
    hits_confirmed: Literal[False]
    affinity_validated: Literal[False]
    scope: Literal["bounded_structural_qc_not_affinity"]


class PublishedFile(StrictModel):
    name: str = Field(min_length=1, max_length=1000)
    artifact_id: str = Field(min_length=1, max_length=200)
    version_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    sha256: Hash
    bytes: int = Field(ge=0, le=128 * 1024 * 1024)


class Delivery(StrictModel):
    schema_name: Literal["refinement-publication-observation-v1"] = Field(alias="schema")
    workflow_id: str = Field(min_length=1, max_length=100)
    workflow_digest: Hash
    task_id: str = Field(pattern=r"^screen-[a-f0-9]{24}$")
    checked_at: int = Field(gt=0)
    state: Literal["NOT_STARTED", "WAITING", "ACTIVE", "PAUSED", "COMPLETE"]
    verification: Literal["verified", "not_ready", "evidence_rejected", "verification_unavailable"]
    report_digest: Hash | None
    inventory_digest: Hash | None
    files: list[PublishedFile] = Field(max_length=2000)
    compute_submitted: Literal[False]
    platform_files_published: Literal[False]
    md_ready: Literal[False]


class Result(StrictModel):
    schema_name: Literal["library-workflow-refinement-result-v1"] = Field(alias="schema")
    reference: dict
    task_id: str = Field(pattern=r"^screen-[a-f0-9]{24}$")
    checked_at: int = Field(gt=0)
    ledger_sequence: int = Field(gt=0)
    verification: Literal["verified", "not_ready", "evidence_rejected", "verification_unavailable"]
    report: Report | None
    delivery: Delivery | None = None
    compute_submitted: Literal[False]
    scientific_assessment: Literal["not_evaluated"]


def verify(state, value, observed):
    Result.model_validate(value)
    Observation.model_validate(observed)
    current = state["refinement_handoff"]
    identity = current["draft_identity"]
    expected = {"planning_scope": current["planning_scope"], **{k: identity[k] for k in ("workflow_id", "digest")}} if identity else None
    previous = current.get("report_result")
    if (not expected or value["reference"] != expected or value["task_id"] != current["task_id"]
        or not observed or value["ledger_sequence"] != observed["ledger_sequence"]
        or value["reference"] != observed["reference"]
        or previous and (value["checked_at"] < previous["checked_at"] or value["ledger_sequence"] < previous["ledger_sequence"])
        or (value["verification"] == "verified") != (value["report"] is not None)):
        raise ValueError("Refinement report differs from its bound execution or fresh observation")
    report = value["report"]
    delivery = value.get("delivery")
    if delivery is not None:
        if (report is None or delivery["workflow_id"] != expected["workflow_id"]
            or delivery["workflow_digest"] != expected["digest"] or delivery["task_id"] != current["task_id"]
            or delivery["checked_at"] > value["checked_at"]
            or previous and previous.get("delivery") and delivery["checked_at"] < previous["delivery"]["checked_at"]):
            raise ValueError("Publication inspection differs from its bound report")
        files = delivery["files"]
        if delivery["verification"] != "verified":
            if files:
                raise ValueError("Unverified publication cannot provide usable file references")
        elif (delivery["state"] != "COMPLETE" or not files or not delivery["inventory_digest"]
            or delivery["report_digest"] != report["report_digest"]
            or len({f["project_id"] for f in files}) != 1
            or files[0]["project_id"] != current["host_scope"]["project"]
            or any(len({f[k] for f in files}) != len(files) for k in ("name", "artifact_id", "version_id"))
            or any("\\" in f["name"] or any(ord(c) < 32 or ord(c) == 127 for c in f["name"])
                or any(part in {"", ".", ".."} for part in f["name"].split("/")) for f in files)
            or sum(f["bytes"] for f in files) > 1024 * 1024 * 1024
            or current.get("delivery_witness") and {k: v for k, v in delivery.items() if k != "checked_at"} != current["delivery_witness"]):
            raise ValueError("Publication files differ from the verified delivery witness")
    if report is None:
        return
    selection = state["screening_handoff"]["selection"]
    if (observed["execution_state"] != "completed" or not observed["approval_recorded"]
        or report["candidate_count"] != current["selected_count"]
        or report["input_identity_sha256"] != selection["identity_sha256"]
        or report["quality_policy_digest"] != current["receipt"]["proposal"]["quality_policy_digest"]
        or [{k: row[k] for k in SelectedCandidate.model_fields} for row in report["candidates"]] != selection["candidates"]
        or [(s["engine"], s["task_id"]) for s in report["stages"]] != [(s["engine"], s["task_id"]) for s in observed["stages"]]
        or current.get("report_witness") and report != current["report_witness"]):
        raise ValueError("Refinement report replaced the frozen candidates, policy or result witness")
    statuses = ("BOTH_STRUCTURAL_QC_PASSED", "STRUCTURAL_QC_DISAGREEMENT", "BOTH_STRUCTURAL_QC_REJECTED", "NOT_FULLY_EVALUATED")
    if (len(report["candidates"]) != report["candidate_count"]
        or report["counts"] != {s: sum(row["status"] == s for row in report["candidates"]) for s in statuses}):
        raise ValueError("Refinement report denominators differ")
    strict = report["quality_policy_digest"] == validate_policy(STRICT_POLICY)
    for row in report["candidates"]:
        for name in ("af3", "covdock"):
            engine = row[name]
            quality = engine["quality"]
            assessment = quality.get("assessment")
            if strict != (assessment is not None):
                raise ValueError("Independent quality axes differ from the frozen policy")
            if assessment is not None:
                failed = engine["status"] == "FAILED"
                failures, missing = assessment["failure_reasons"], assessment["missing_reasons"]
                physical = "NOT_EVALUATED" if failed else "FAIL" if failures else "UNKNOWN" if missing else "PASS"
                if (assessment["physical_validity"] != physical
                    or assessment["review_eligible"] != (not failed and physical == "PASS")
                    or assessment["evidence_complete"] != (not failed and not missing)
                    or failed and (failures or missing)
                    or not failed and (quality["status"] == "PASSED") != assessment["review_eligible"]
                    or not failed and sorted(filter(None, (quality["reason"] or "").split("; "))) != sorted(failures + missing)):
                    raise ValueError("Independent quality axes contradict the retained outcome")
            metrics = engine["measurements"]
            if strict and (engine["status"] == "FAILED") != (metrics is None):
                raise ValueError("Strict quality measurements must preserve engine execution")
            if name == "covdock" and metrics is not None:
                provenance = metrics.get("strain_provenance")
                if strict != (provenance is not None) or provenance is not None and (
                    provenance["status"] == "VALID") != (metrics["strain_kcal_mol"] is not None):
                    raise ValueError("Strain provenance differs from the frozen policy or value")
            if (engine["measurements"] is not None and engine["measurements"]["engine"] != name
                or (engine["status"] == "FAILED") != (quality["status"] == "NOT_EVALUATED")
                or (engine["status"] == "FAILED") != (engine["score"] is None)
                or (quality["status"] == "REJECTED") != (quality["reason"] is not None)):
                raise ValueError("Refinement candidate engine evidence is inconsistent")
        expected_status = (statuses[3] if any(row[e]["status"] == "FAILED" or
            (row[e]["quality"].get("assessment") or {}).get("physical_validity") == "UNKNOWN" for e in ("af3", "covdock"))
            else statuses[0] if all(row[e]["quality"]["status"] == "PASSED" for e in ("af3", "covdock"))
            else statuses[1] if any(row[e]["quality"]["status"] == "PASSED" for e in ("af3", "covdock")) else statuses[2])
        if row["status"] != expected_status:
            raise ValueError("Refinement combined verdict conflicts with its engine verdicts")
    for stage in report["stages"]:
        success = sum(row[stage["engine"]]["status"] == "SUCCESS" for row in report["candidates"])
        if stage["success_count"] != success or stage["failed_count"] != report["candidate_count"] - success:
            raise ValueError("Refinement engine coverage differs from retained candidates")


def commit(folder, state, value):
    current = state["refinement_handoff"]
    if current.get("report_result") == value:
        return
    current["report_result"] = deepcopy(value)
    if value.get("delivery") and value["delivery"]["verification"] == "verified":
        current["delivery_witness"] = deepcopy({k: v for k, v in value["delivery"].items() if k != "checked_at"})
    if value["report"] is not None:
        current["report_witness"] = deepcopy(value["report"])
        state["stage"] = "REFINEMENT_CANDIDATE_REVIEW"
    elif state["stage"] == "REFINEMENT_CANDIDATE_REVIEW":
        state["stage"] = "REFINEMENT_REVIEW"
    changed(folder, state, "refinement_report_observed")
