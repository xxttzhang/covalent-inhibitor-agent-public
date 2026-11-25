"""Explicit MD candidate decisions over verified native publication identities.

This records a research choice only. Asset transfer, pose freezing and native
graph admission must independently validate the same parents and actual bytes.
"""
from copy import deepcopy
from uuid import uuid4

from pydantic import Field
from covscreen.workflow_protocol import StrictModel, archive_branch, changed
from .refinement_results import Hash, PublishedFile

ACTIONS = {"select_md_candidate", "release_md_candidate"}
TABLE = "report/candidate-results.csv"
STRUCTURE = "covdock/covdock_lo/library_first_core_subset_covdock_lo-out.maegz"


class Candidate(StrictModel):
    compound_id: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
    input_sdf_index: int = Field(ge=0)


class Context(StrictModel):
    id: str = Field(pattern=r"^receptor-context-[a-f0-9]{32}$")
    sha256: Hash


class Parents(StrictModel):
    preparation_run_id: str = Field(min_length=1, max_length=200)
    prepared_receptor_manifest_sha256: Hash
    receptor_context: Context


def parents(state):
    prep = state.get("preparation") or {}
    if prep.get("state") != "COMPLETED":
        raise ValueError("MD candidate requires the completed receptor preparation")
    return Parents.model_validate({"preparation_run_id": prep.get("run_id"),
        "prepared_receptor_manifest_sha256": prep.get("manifest_sha256"),
        "receptor_context": state.get("receptor_context")}).model_dump()


def available(state, value=None):
    value = value or (state.get("refinement_handoff") or {}).get("report_result")
    if not value or value.get("verification") != "verified" or not value.get("report"):
        return False
    delivery = value.get("delivery")
    if (not delivery or delivery["verification"] != "verified" or delivery["state"] != "COMPLETE"
        or not {TABLE, STRUCTURE}.issubset({f["name"] for f in delivery["files"]})
        or not any(c["status"] == "BOTH_STRUCTURAL_QC_PASSED" for c in value["report"]["candidates"])):
        return False
    try:
        parents(state)
    except ValueError:
        return False
    return True


def validate_arguments(values):
    reason = values.get("reason")
    minimum = 20 if values["action"] == "select_md_candidate" else 5
    if not isinstance(reason, str) or not minimum <= len(reason.strip()) <= 2000:
        raise ValueError("MD candidate decision requires a concrete scientific reason")
    if values["action"] == "select_md_candidate":
        Candidate.model_validate({k: values.get(k) for k in Candidate.model_fields})


def select(folder, state, values, result):
    current = state["refinement_handoff"]
    if (result != current.get("report_result") or not available(state, result)
        or (current.get("execution_observation") or {}).get("execution_state") != "completed"):
        raise ValueError("MD candidate requires the current freshly verified report and native files")
    candidate = Candidate.model_validate({k: values[k] for k in Candidate.model_fields}).model_dump()
    matches = [row for row in result["report"]["candidates"] if all(row[k] == v for k, v in candidate.items())]
    if len(matches) != 1 or matches[0]["status"] != "BOTH_STRUCTURAL_QC_PASSED":
        raise ValueError("Select an exact candidate that passed both structural-quality gates")
    files = {f["name"]: PublishedFile.model_validate(f).model_dump() for f in result["delivery"]["files"]}
    binding = {"candidate": candidate, "reason": values["reason"].strip(),
        "native_workflow_id": result["reference"]["workflow_id"], "native_workflow_digest": result["reference"]["digest"],
        "task_id": result["task_id"], "report_digest": result["report"]["report_digest"],
        "files": {"candidate_table": files[TABLE], "covdock_structure": files[STRUCTURE]}, "parents": parents(state)}
    previous = state.get("md_candidate")
    if previous:
        if any(previous[k] != value for k, value in binding.items()):
            raise ValueError("MD candidate is already bound; explicitly release it before changing the decision")
        return
    # No paths, MD-ready flag or compute approval can be supplied by the model.
    state["md_candidate"] = {"schema": "library-md-candidate-choice-v1", "id": "md-choice-" + uuid4().hex,
        **deepcopy(binding), "selected_at": result["checked_at"]}
    changed(folder, state, "md_candidate_selected", {"choice_id": state["md_candidate"]["id"]})


def release(folder, state, reason):
    if not state.get("md_candidate"):
        raise ValueError("No MD candidate decision to release")
    if any(state.get(key) for key in ("md_transfer", "md_input", "md_execution")):
        raise ValueError("MD inputs or execution already bind this decision; preserve them in an explicit downstream branch")
    archive_branch(state, reason.strip(), retain=("md_candidate",), invalidate=("md_candidate",))
    changed(folder, state, "md_candidate_released", {"reason": reason.strip()})


def public(state):
    choice = state.get("md_candidate")
    if not choice:
        return None
    if choice.get('schema') == 'library-historical-md-choice-v1':
        return {k: deepcopy(choice[k]) for k in ('id', 'candidate', 'reason', 'source_manifest_sha256')} | {
            'state': 'historical_input_frozen' if state.get('historical_md_source', {}).get('verification') == 'source_chain_verified'
                     else 'selected_evidence_reverification_required', 'md_ready': False, 'compute_submitted': False}
    result = (state.get("refinement_handoff") or {}).get("report_result")
    verified = available(state, result)
    if verified:
        files = {f["name"]: f for f in result["delivery"]["files"]}
        verified = (choice["report_digest"] == result["report"]["report_digest"]
            and choice["native_workflow_id"] == result["reference"]["workflow_id"]
            and choice["native_workflow_digest"] == result["reference"]["digest"] and choice["task_id"] == result["task_id"]
            and choice["files"] == {"candidate_table": files[TABLE], "covdock_structure": files[STRUCTURE]}
            and choice["parents"] == parents(state))
    return {k: deepcopy(choice[k]) for k in ("id", "candidate", "reason", "report_digest", "selected_at")} | {
        "state": ("selected_input_frozen" if state.get("md_input") else "selected_asset_handoff_pending") if verified else "selected_evidence_reverification_required",
        "md_ready": False, "compute_submitted": False}
