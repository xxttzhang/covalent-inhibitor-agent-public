"""Persist the reviewed research choice before native refinement planning.

Only the authenticated screening host exchange calls these helpers, under the
existing workflow lock. Native proposal/compiler code remains authoritative.
"""
from copy import deepcopy
from typing import Literal
from uuid import uuid4

from pydantic import Field
from covscreen.workflow_protocol import StrictModel, changed
from .screening_results import binding
from . import refinement_execution, md_selection, md_transfer

ACTIONS = {"plan_refinement", "recover_refinement", "release_refinement", "refinement_report", "resume_refinement_delivery", *refinement_execution.ACTIONS, *md_selection.ACTIONS, *md_transfer.ACTIONS}
PHASES = {"REFINEMENT_HANDOFF", "REFINEMENT_PLAN_READY", "REFINEMENT_PLAN_BLOCKED", *refinement_execution.PHASES}


class Stage(StrictModel):
    engine: Literal["af3", "covdock"]
    minutes: int = Field(gt=0)
    candidate_count: int = Field(ge=1, le=100)
    proposed_points: float = Field(ge=0, allow_inf_nan=False)


class Proposal(StrictModel):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["BLOCKED", "PENDING_RUNTIME_CONFIRMATION"]
    proposed_points: float = Field(ge=0, allow_inf_nan=False)
    selected_count: int = Field(ge=1, le=100)
    quality_policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    blockers: list[str] = Field(max_length=30)
    stages: list[Stage] = Field(min_length=2, max_length=2)


class Draft(StrictModel):
    workflow_id: str = Field(pattern=r"^refineflow-[a-f0-9]{24}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    proposal_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["COMPILED_NOT_EXECUTABLE", "EXPIRED"]
    approve_before: int = Field(gt=0)
    blockers: list[str] = Field(max_length=30)


class PlanningReceipt(StrictModel):
    schema_name: Literal["library-workflow-refinement-planning-v1"] = Field(alias="schema")
    planning_scope: dict
    task_id: str = Field(pattern=r"^screen-[a-f0-9]{24}$")
    checked_at: int = Field(gt=0)
    proposal: Proposal | None
    draft: Draft | None
    compute_submitted: Literal[False]
    approval_recorded: Literal[False]
    scientific_assessment: Literal["not_evaluated"]


def validate_arguments(values):
    if values["action"] in md_selection.ACTIONS:
        md_selection.validate_arguments(values)
        return
    if values["action"] != "plan_refinement":
        return
    review = values["refinement_review"]
    cap = values["max_points"]
    if not isinstance(review, str) or not 20 <= len(review.strip()) <= 4000:
        raise ValueError("Refinement requires a concrete scientific review")
    if type(cap) not in (int, float) or not 0 <= cap < float("inf"):
        raise ValueError("Refinement requires an independent finite proposed budget")


def open_operation(folder, state, arguments, scope, original):
    current = state.get("refinement_handoff")
    if current:
        if current["host_scope"] != scope or original is not None and current["source"] != original:
            raise ValueError("Refinement operation belongs to another host or original request")
        if arguments["action"] == "plan_refinement" and (
            arguments["max_points"] != current["planning_scope"]["max_points"]
            or arguments["refinement_review"].strip() != current["planning_scope"]["scientific_review"]):
            raise ValueError("Refinement operation already binds its review and budget")
        return current
    handoff = state["screening_handoff"]
    result = handoff.get("screening_result")
    observed = handoff.get("observation")
    if (arguments["action"] != "plan_refinement" or state["stage"] != "CANDIDATE_REVIEW" or original is None
        or not result or result["verification"] != "verified" or not result["selection"]
        or result["selection"]["selected_count"] < 1
        or result["selection"] != handoff.get("selection")
        or not observed or observed["task_state"] != "SUCCEEDED" or not observed["approval_recorded"]
        or result["reference"] != observed["reference"] or result["task_id"] != observed["task_id"]):
        raise ValueError("Refinement planning requires the completed parent's currently reviewed nonempty subset")
    current = {"host_scope": deepcopy(scope), "source": deepcopy(original), "task_id": observed["task_id"],
        "planning_scope": {"schema": "bound-refinement-planning-v1", "operation_id": uuid4().hex,
            "parent": deepcopy(observed["reference"]), "selection": deepcopy(binding(result["selection"])),
            "max_points": arguments["max_points"], "scientific_review": arguments["refinement_review"].strip()},
        "selected_count": result["selection"]["selected_count"], "receipt": None, "proposal_digest": None, "draft_identity": None}
    state.update(refinement_handoff=current, stage="REFINEMENT_HANDOFF")
    changed(folder, state, "refinement_host_bound")
    return current


def commit(folder, state, value):
    PlanningReceipt.model_validate(value)
    current = state["refinement_handoff"]
    if (current.get("execution_observation") or {}).get("native_state", "NOT_CREATED") != "NOT_CREATED":
        raise ValueError("Native refinement lifecycle cannot return to planning")
    if value["planning_scope"] != current["planning_scope"] or value["task_id"] != current["task_id"]:
        raise ValueError("Refinement receipt differs from the locked research operation")
    previous = current["receipt"]
    if previous and (value["checked_at"] < previous["checked_at"] or
        value["checked_at"] == previous["checked_at"] and value != previous):
        raise ValueError("Stale or conflicting refinement planning receipt")
    proposal, draft = value["proposal"], value["draft"]
    if proposal:
        if (proposal["selected_count"] != current["selected_count"]
            or [s["engine"] for s in proposal["stages"]] != ["af3", "covdock"]
            or any(s["candidate_count"] != current["selected_count"] for s in proposal["stages"])
            or abs(sum(s["proposed_points"] for s in proposal["stages"]) - proposal["proposed_points"]) > 1e-8
            or current["proposal_digest"] and proposal["digest"] != current["proposal_digest"]
            or (proposal["state"] == "BLOCKED") != bool(proposal["blockers"])
            or proposal["proposed_points"] > current["planning_scope"]["max_points"] + 1e-8
                and "PROPOSED_REFINEMENT_BUDGET_EXCEEDED" not in proposal["blockers"]):
            raise ValueError("Refinement proposal changed candidates, identity or budget")
    if draft:
        identity = {k: draft[k] for k in ("workflow_id", "digest", "proposal_digest", "approve_before")}
        if (not proposal or proposal["state"] != "PENDING_RUNTIME_CONFIRMATION"
            or draft["proposal_digest"] != proposal["digest"]
            or (draft["state"] == "EXPIRED") != (value["checked_at"] > draft["approve_before"])
            or current["draft_identity"] and identity != current["draft_identity"]):
            raise ValueError("Refinement draft cannot replace the committed proposal or plan")
    if proposal:
        current["proposal_digest"] = proposal["digest"]
    if draft:
        current["draft_identity"] = identity
    if previous != value:
        current["receipt"] = deepcopy(value)
        state["stage"] = "REFINEMENT_PLAN_READY" if draft and draft["state"] == "COMPILED_NOT_EXECUTABLE" and not draft["blockers"] else "REFINEMENT_PLAN_BLOCKED"
        changed(folder, state, "refinement_plan_observed")


def public(state):
    current = state.get("refinement_handoff")
    if not current:
        return None
    receipt = current["receipt"]
    observed = current.get("execution_observation")
    return {"operation_id": current["planning_scope"]["operation_id"],
        "selection_id": current["planning_scope"]["selection"]["selection_id"],
        "scientific_review": current["planning_scope"]["scientific_review"],
        "proposed_max_points": current["planning_scope"]["max_points"],
        "proposal": receipt["proposal"] if receipt else None, "draft": receipt["draft"] if receipt else None,
        "planning_status": "awaiting_host" if not receipt else "proposal_unavailable" if not receipt["proposal"] else
            "proposal_blocked" if receipt["proposal"]["blockers"] else "draft_unavailable" if not receipt["draft"] else
            "draft_expired" if receipt["draft"]["state"] == "EXPIRED" else "interface_blocked" if receipt["draft"]["blockers"] else "compiled",
        "observation_scope": "persisted_native_planning", "current_resource_availability_verified": False,
        "compute_submitted": (True if any(s["job_observed"] for s in observed["stages"])
            else None if any(s["submission_reserved"] for s in observed["stages"]) else False) if observed else False,
        "approval_recorded": observed["approval_recorded"] if observed else False, "scientific_assessment": "not_evaluated"}


def release(folder, state, value):
    current = state["refinement_handoff"]
    if (current.get("execution_observation") or {}).get("approval_recorded"):
        raise ValueError("An approved refinement cannot release its frozen research choices")
    if (not isinstance(value, dict) or set(value) != {"schema", "planning_scope", "task_id", "reason", "state", "compute_submitted"}
        or value["schema"] != "library-refinement-planning-closure-v1" or value["state"] != "NO_EXECUTABLE_WORKFLOW"
        or value["compute_submitted"] is not False or value["planning_scope"] != current["planning_scope"]
        or value["task_id"] != current["task_id"] or not isinstance(value["reason"], str)
        or not 5 <= len(value["reason"].strip()) <= 2000):
        raise ValueError("Refinement release requires the exact sealed native planning operation")
    state.setdefault("refinement_handoff_history", []).append({**deepcopy(current), "closure": deepcopy(value)})
    state.update(refinement_handoff=None, stage="CANDIDATE_REVIEW")
    changed(folder, state, "refinement_planning_released")
