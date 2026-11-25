"""Durable scientific intent for the existing host screening compiler.

This is a research proposal, not native user approval or a second task ledger.
The workflow commits its reference before any host may compile an execution.
"""
from copy import deepcopy
import csv
import hashlib
import json
import math
from pathlib import Path
import re

from covscreen.jobs import ArtifactRegistry
from covscreen.receptor_evidence import regular
from . import receptor_handoff, screening_inputs

SCHEMA = "library-screening-plan-v1"
REVIEW_FIELDS = ("construct_and_components", "reaction_scope", "prefix_scope")


def proposal(review, max_points):
    if not isinstance(review, dict) or set(review) != set(REVIEW_FIELDS):
        raise ValueError("Scientific review must address construct/components, reaction and prefix scope")
    if any(not isinstance(value, str) or not 20 <= len(value.strip()) <= 4000 for value in review.values()):
        raise ValueError("Each scientific review requires a concrete rationale of 20-4000 characters")
    if type(max_points) not in (int, float) or not math.isfinite(max_points) or max_points < 0:
        raise ValueError("A finite nonnegative proposed point budget is required; it is not resource approval")
    return {key: review[key].strip() for key in REVIEW_FIELDS}


def conditions():
    # This is the sole supported profile, already admitted by screening_inputs.
    # Do not infer these values for another target or structure.
    return {"target": "EGFR", "site": "C797", "target_parameters": {
        "target": "EGFR", "receptor": "1M17", "chain": "A", "residue": "CYS", "residue_number": 797,
        "insertion_code": "", "numbering": "canonical", "reaction_type": "Michael Addition"},
        "library": "CovInDB/Virtual_Screening_Library.csv", "screening_mode": "research_pilot_v1"}


def folder(root, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"screening-plan-[a-f0-9]{32}", identifier):
        raise ValueError("Invalid screening plan identity")
    result = root / "state/screening_plans" / identifier[15:]
    if any(path.is_symlink() for path in (root / "state", result.parent, result)):
        raise ValueError("Screening plan directory cannot be a symbolic link")
    return result


def present(identifier, checksum, record):
    intent = record["intent"]
    return {"artifact_id": identifier, "manifest_sha256": checksum,
        "operation_id": intent["operation_id"], "intent_revision": intent["revision"],
        "conditions": intent["conditions"], "scientific_review": intent["scientific_review"],
        "screening_inputs": intent["screening_inputs"],
        "assessment": "exploratory_proposal_with_recorded_rationale",
        "source_records": 20000, "maximum_shortlist": 5,
        "budget_status": "proposed_not_user_approved", "calculation_submitted": False,
        "screening_authorized": False, "host_handoff_required": True}


def load_record(root, state, *, verify_inputs=True):
    root = Path(root).resolve()
    ref = state.get("screening_plan")
    if not ref:
        raise ValueError("No scientific screening plan is bound to this workflow")
    path, checksum = regular(root, folder(root, ref["id"]) / "screening-plan.json", ref["sha256"])
    if ref["id"] != "screening-plan-" + checksum[:32]:
        raise ValueError("Screening plan content identity changed")
    record = json.loads(path.read_text())
    intent = record["intent"]
    if not verify_inputs:
        # For recovering legacy handoffs only: exact immutable plan bytes still
        # bind identity, but changed/missing scientific files must not prevent
        # status inspection or cancellation of an existing native task.
        if (record.get("schema") != SCHEMA or intent.get("schema") != "library-workflow-screening-intent-v1"
            or intent.get("workflow_id") != state["workflow_id"]
            or type(intent.get("revision")) is not int or not 1 <= intent["revision"] <= state["revision"]
            or not re.fullmatch(r"[a-f0-9]{32}", intent.get("operation_id", ""))):
            raise ValueError("Frozen screening plan identity changed")
        return record
    inputs = screening_inputs.load_record(root, state)
    expected = {"artifact_id": state["screening_inputs"]["id"], "manifest_sha256": state["screening_inputs"]["sha256"]}
    receptor = {"artifact_id": state["screening_receptor"]["id"], "manifest_sha256": state["screening_receptor"]["sha256"],
        "receptor_sha256": inputs["research_inputs"]["receptor"]["sha256"]}
    if (record.get("schema") != SCHEMA or intent.get("schema") != "library-workflow-screening-intent-v1"
        or intent.get("workflow_id") != state["workflow_id"] or intent.get("screening_inputs") != expected
        or intent.get("research_inputs") != inputs["research_inputs"] or intent.get("receptor_evidence") != receptor
        or type(intent.get("revision")) is not int or not 1 <= intent["revision"] <= state["revision"]
        or not re.fullmatch(r"[a-f0-9]{32}", intent.get("operation_id", ""))):
        raise ValueError("Screening plan differs from the bound workflow, revision or evidence")
    review = proposal(intent["scientific_review"], intent["conditions"]["max_points"])
    if review != intent["scientific_review"] or intent["conditions"] != {**conditions(), "max_points": intent["conditions"]["max_points"]}:
        raise ValueError("Screening plan differs from the supported scientific profile")
    return record


def read(root, state):
    record = load_record(root, state)
    ref = state["screening_plan"]
    return present(ref["id"], ref["sha256"], record)


def intent(root, state):
    """Internal only; callers must authenticate the backend before compilation.

    Never return the file roles through a model-visible workflow result.
    """
    return deepcopy(load_record(root, state)["intent"])


def freeze(root, state, review, max_points):
    # Scientific parser dependencies are loaded only for this action, not while
    # advertising the MCP registry or serving legacy capability requests.
    from .covindb import validate_library_columns

    root = Path(root).resolve()
    review = proposal(review, max_points)
    inputs = screening_inputs.load_record(root, state)
    if inputs["binding"]["profile"] != screening_inputs.PROFILE:
        raise ValueError("Unsupported scientific screening profile")
    receptor = receptor_handoff.load_record(root, state)
    if (state["target"] != {"target": "EGFR", "taxon": 9606} or state["site_selection"]["site"] != "C797"
        or receptor["result"]["identity"] != {"entry_id": "1M17", "uniprot": "P00533", "canonical_cys": 797,
            "author_chain": "A", "author_residue": "773"}):
        raise ValueError("Scientific screening plan requires the supported target and receptor evidence")
    with Path(inputs["research_inputs"]["library"]["path"]).open(encoding="utf-8-sig", newline="") as stream:
        validate_library_columns(csv.DictReader(stream).fieldnames)
    if state.get("screening_plan"):
        saved = load_record(root, state)
        if saved["intent"]["scientific_review"] == review and saved["intent"]["conditions"]["max_points"] == max_points:
            return present(state["screening_plan"]["id"], state["screening_plan"]["sha256"], saved)
    pending = state.get("pending") or {}
    if pending.get("action") != "prepare_screening_plan" or not re.fullmatch(r"[a-f0-9]{32}", pending.get("operation_id", "")):
        raise ValueError("Scientific intent requires a durable workflow operation before preparation")
    value = {"schema": "library-workflow-screening-intent-v1", "workflow_id": state["workflow_id"],
        "revision": state["revision"] + 1, "operation_id": pending["operation_id"],
        "conditions": {**conditions(), "max_points": max_points}, "scientific_review": review,
        "screening_inputs": {"artifact_id": state["screening_inputs"]["id"], "manifest_sha256": state["screening_inputs"]["sha256"]},
        "research_inputs": inputs["research_inputs"], "receptor_evidence": {
            "artifact_id": state["screening_receptor"]["id"], "manifest_sha256": state["screening_receptor"]["sha256"],
            "receptor_sha256": inputs["research_inputs"]["receptor"]["sha256"]}}
    record = {"schema": SCHEMA, "intent": value, "screening_authorized": False, "calculation_submitted": False}
    # Same byte serialization as ArtifactRegistry._write_json, without a second
    # cross-language JSON hash convention. Host digests its parsed intent itself.
    content = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
    checksum = hashlib.sha256(content).hexdigest()
    identifier = "screening-plan-" + checksum[:32]
    destination = folder(root, identifier)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "screening-plan.json"
    if path.exists():
        regular(root, path, checksum)
    else:
        ArtifactRegistry._write_json(path, record)
    return present(identifier, checksum, record)
