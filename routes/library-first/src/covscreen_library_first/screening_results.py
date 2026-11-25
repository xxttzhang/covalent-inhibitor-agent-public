"""Bounded host report receipts; scientific ranking stays in the native worker."""
from typing import Literal
from pydantic import Field, model_validator
from covscreen.workflow_protocol import StrictModel

RESULT_ACTIONS = {"screening_report", "select_screening_candidates", "read_screening_candidates"}


class SelectionPolicy(StrictModel):
    max_candidates: int = Field(ge=1, le=100)
    max_fraction: float = Field(gt=0, le=0.25)
    disagreement_threshold: float = Field(gt=0, le=1)
    top_fraction: float = Field(gt=0, le=1)
    uncertainty_count: int = Field(ge=1, le=100)
    consensus_count: int = Field(ge=0, le=100)
    diversity_count: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def quota(self):
        if self.uncertainty_count + self.consensus_count + self.diversity_count > self.max_candidates:
            raise ValueError("Selection channel quotas exceed the total cap")
        return self


class Candidate(StrictModel):
    input_sdf_index: int = Field(ge=0)
    compound_id: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
    selection_channel: Literal["rank_disagreement_review", "consensus_sentinel", "diversity_sentinel"]
    carsidock_rank_fraction: float = Field(ge=0, le=1)
    rtmscore_rank_fraction: float = Field(ge=0, le=1)
    rank_disagreement: float = Field(ge=0, le=1)


class Selection(StrictModel):
    selection_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    selection_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy: SelectionPolicy
    reason: str = Field(min_length=20, max_length=2000)
    status: Literal["SUBSET_PROPOSED", "NO_CANDIDATES_MEET_POLICY"]
    input_count: int = Field(ge=0, le=20000)
    eligible_count: int = Field(ge=0, le=20000)
    excluded_count: int = Field(ge=0, le=20000)
    selected_count: int = Field(ge=0, le=100)
    effective_cap: int = Field(ge=0, le=100)
    candidates: list[Candidate] = Field(max_length=100)
    calibrated_uncertainty: Literal[False]
    refinement_approval_required: Literal[True]
    limitations: list[str] = Field(min_length=1, max_length=20)


class Stage(StrictModel):
    phase: Literal["prepare", "carsidock", "rtmscore", "report"]
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_count: int = Field(ge=0, le=20000)
    success_count: int = Field(ge=0, le=20000)
    failed_count: int = Field(ge=0, le=20000)
    artifact_count: int = Field(gt=0)


class ScreeningResult(StrictModel):
    schema_name: Literal["library-workflow-screening-result-v1"] = Field(alias="schema")
    reference: dict
    task_id: str = Field(pattern=r"^screen-[a-f0-9]{24}$")
    checked_at: int = Field(gt=0)
    verification: Literal["verified", "not_ready", "evidence_rejected", "verification_unavailable", "selection_unavailable"]
    report_verified_at: int | None = Field(gt=0)
    stages: list[Stage] = Field(max_length=4)
    selection: Selection | None
    compute_submitted: Literal[False]
    scientific_assessment: Literal["not_evaluated"]


def binding(selection):
    return {k: selection[k] for k in ("selection_id", "selection_manifest_digest", "policy", "reason")} if selection else None


def phase(handoff, fallback):
    if fallback != "SCREENING_REVIEW":
        return fallback
    result = handoff.get("screening_result")
    if not result or result["verification"] != "verified" or not result["selection"]:
        return fallback
    return "CANDIDATE_REVIEW" if result["selection"]["selected_count"] else "SCREENING_SELECTION_EMPTY"


def verify(value, observed, handoff, arguments):
    # This validates the receipt contract and its exact parent; the native
    # verifier remains responsible for manifest/file/ranking recomputation.
    ScreeningResult.model_validate(value)
    if (observed["task_state"] != "SUCCEEDED" or not observed["approval_recorded"]
        or any(value[k] != observed[k] for k in ("task_id", "reference"))):
        raise ValueError("Screening result differs from the completed native operation")
    previous = handoff.get("screening_result")
    if previous and value["checked_at"] < previous["checked_at"]:
        raise ValueError("Stale result read cannot replace a later observation")
    if value["compute_submitted"] is not False:
        raise ValueError("Result inspection cannot submit compute")
    stages = value["stages"]
    if value["report_verified_at"] is not None:
        if ([s["phase"] for s in stages] != ["prepare", "carsidock", "rtmscore", "report"]
            or any(s["input_count"] != s["success_count"] + s["failed_count"] for s in stages)):
            raise ValueError("Verified report must preserve every phase and its denominator")
        if handoff.get("report_witness") and handoff["report_witness"] != stages:
            raise ValueError("A completed operation cannot replace its bound report evidence")
    elif stages or value["verification"] in {"verified", "selection_unavailable"}:
        raise ValueError("Unavailable report cannot expose verified results")
    chosen = value["selection"]
    if chosen:
        if value["verification"] != "verified" or chosen["source_manifest_digest"] != stages[-1]["manifest_digest"]:
            raise ValueError("Selection belongs to another report or is not currently available")
        if (chosen["input_count"] != stages[-1]["input_count"]
            or chosen["input_count"] != chosen["eligible_count"] + chosen["excluded_count"]
            or chosen["eligible_count"] > stages[-1]["success_count"]
            or chosen["selected_count"] != len(chosen["candidates"])
            or chosen["selected_count"] > chosen["effective_cap"]
            or (chosen["status"] == "SUBSET_PROPOSED") != bool(chosen["selected_count"])):
            raise ValueError("Candidate result lost denominator, exclusions or empty status")
        if (len({row["input_sdf_index"] for row in chosen["candidates"]}) != len(chosen["candidates"])
            or len({row["compound_id"] for row in chosen["candidates"]}) != len(chosen["candidates"])
            or not all(1 <= len(item) <= 1000 for item in chosen["limitations"])):
            raise ValueError("Candidate identities must be unique and limitations bounded")
        if chosen["calibrated_uncertainty"] is not False or chosen["refinement_approval_required"] is not True:
            raise ValueError("Selection is an uncalibrated proposal requiring separate refinement approval")
        if arguments["action"] == "select_screening_candidates":
            if chosen["policy"] != arguments["selection_policy"] or chosen["reason"] != arguments["reason"].strip():
                raise ValueError("Candidate policy or rationale differs from the requested review")
        elif binding(chosen) != binding(handoff.get("selection")):
            raise ValueError("Read must restore the exact bound subset, never adopt the latest proposal")
    elif value["verification"] == "verified" and (handoff.get("selection") or arguments["action"] != "screening_report"):
        raise ValueError("Selection read cannot silently omit its bound subset")
    return value
