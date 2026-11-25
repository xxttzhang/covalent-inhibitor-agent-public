"""Admit compact native ledger facts; never authorize or simulate execution."""
from copy import deepcopy
from typing import Literal
from pydantic import Field
from covscreen.workflow_protocol import StrictModel, changed

ACTIONS = {"approve_refinement", "refinement_status", "cancel_refinement", "wake_refinement"}
PHASES = {"REFINEMENT_APPROVAL_PENDING", "REFINEMENT_EXECUTION", "REFINEMENT_STOPPED", "REFINEMENT_REVIEW", "REFINEMENT_CANDIDATE_REVIEW"}


class Stage(StrictModel):
    task_id: str = Field(pattern=r"^refine-[a-f0-9]{24}$")
    engine: Literal["af3", "covdock"]
    state: Literal["NOT_CREATED", "DRAFT", "APPROVED", "WAITING", "DISPATCHING", "SUBMITTED", "QUEUED", "RUNNING",
        "VERIFYING", "NEEDS_REVIEW", "CANCEL_REQUESTED", "CANCELLED", "FAILED", "SUCCEEDED"]
    submission_reserved: bool
    job_observed: bool
    result_record_verified: bool


class Observation(StrictModel):
    schema_name: Literal["library-refinement-ledger-observation-v1"] = Field(alias="schema")
    reference: dict
    task_id: str = Field(pattern=r"^screen-[a-f0-9]{24}$")
    native_state: Literal["NOT_CREATED", "DRAFT", "APPROVED", "CANCELLED"]
    approval_recorded: bool
    execution_state: Literal["not_submitted", "submitted", "running", "completed", "failed", "outcome_unknown"]
    stages: list[Stage] = Field(min_length=2, max_length=2)
    ledger_sequence: int = Field(gt=0)
    observed_at: int = Field(gt=0)
    observation_scope: Literal["persisted_executor_ledger"]
    remote_artifacts_rehashed: Literal[False]
    scientific_assessment: Literal["not_evaluated"]


def observe(folder, state, value):
    Observation.model_validate(value)
    current = state["refinement_handoff"]
    identity = current["draft_identity"]
    expected = {"planning_scope": current["planning_scope"],
        **({k: identity[k] for k in ("workflow_id", "digest")} if identity else {})}
    if not identity or value["reference"] != expected or value["task_id"] != current["task_id"]:
        raise ValueError("Refinement observation differs from the bound draft or parent")
    stages = value["stages"]
    if ([s["engine"] for s in stages] != ["af3", "covdock"] or len({s["task_id"] for s in stages}) != 2
        or value["approval_recorded"] != (value["native_state"] == "APPROVED")):
        raise ValueError("Refinement member or approval evidence is inconsistent")
    for stage in stages:
        if (stage["result_record_verified"] != (stage["state"] == "SUCCEEDED")
            or stage["job_observed"] and not stage["submission_reserved"]
            or stage["state"] in {"SUBMITTED", "QUEUED", "RUNNING", "VERIFYING", "SUCCEEDED"} and not stage["job_observed"]
            or stage["state"] in {"DISPATCHING", "SUBMITTED", "QUEUED", "RUNNING", "VERIFYING", "SUCCEEDED"} and not stage["submission_reserved"]
            or stage["state"] in {"NOT_CREATED", "DRAFT", "APPROVED", "WAITING"} and stage["submission_reserved"]
            or value["approval_recorded"] and stage["state"] in {"NOT_CREATED", "DRAFT"}
            or not value["approval_recorded"] and (stage["state"] != value["native_state"] or stage["submission_reserved"] or stage["job_observed"])):
            raise ValueError("Refinement execution cannot precede approval or lose its submission provenance")
    states = {s["state"] for s in stages}
    execution = ("not_submitted" if not value["approval_recorded"] else "completed" if states == {"SUCCEEDED"}
        else "failed" if states <= {"SUCCEEDED", "FAILED", "CANCELLED"}
        else "outcome_unknown" if states & {"NEEDS_REVIEW", "DISPATCHING", "CANCEL_REQUESTED"}
        else "running" if states & {"RUNNING", "VERIFYING"} else "submitted" if states & {"SUBMITTED", "QUEUED"}
        else "outcome_unknown" if states & {"FAILED", "CANCELLED"} else "not_submitted")
    if execution != value["execution_state"]:
        raise ValueError("Refinement execution summary conflicts with its native stages")
    previous = current.get("execution_observation")
    if previous:
        if (value["ledger_sequence"] < previous["ledger_sequence"]
            or value["ledger_sequence"] == previous["ledger_sequence"] and any(value[k] != previous[k] for k in value if k != "observed_at")
            or previous["approval_recorded"] and not value["approval_recorded"]
            or previous["native_state"] != "NOT_CREATED" and value["native_state"] == "NOT_CREATED"
            or previous["native_state"] == "CANCELLED" and value["native_state"] != "CANCELLED"):
            raise ValueError("Stale or conflicting refinement ledger observation")
        for old, new in zip(previous["stages"], stages):
            if (old["task_id"] != new["task_id"] or old["engine"] != new["engine"]
                or old["submission_reserved"] and not new["submission_reserved"]
                or old["job_observed"] and not new["job_observed"]
                or old["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"} and old["state"] != new["state"]):
                raise ValueError("Refinement stage identity or terminal evidence changed")
    if previous != value:
        current["execution_observation"] = deepcopy(value)
        if value["native_state"] == "DRAFT":
            state["stage"] = "REFINEMENT_APPROVAL_PENDING"
        elif value["native_state"] == "CANCELLED" or execution == "failed":
            state["stage"] = "REFINEMENT_STOPPED"
        elif value["approval_recorded"]:
            state["stage"] = ("REFINEMENT_CANDIDATE_REVIEW" if (current.get("report_result") or {}).get("verification") == "verified"
                else "REFINEMENT_REVIEW") if execution == "completed" else "REFINEMENT_EXECUTION"
        changed(folder, state, "refinement_ledger_observed", {"ledger_sequence": value["ledger_sequence"]})
