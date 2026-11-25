"""Authenticated host-only planning exchange; never grants compute authority."""
import hashlib
import json
from pathlib import Path
import re
import time

from covscreen.native_approval import decode, signed
from covscreen.ligand_parent_evidence import version_lock
from covscreen.workflow_store import load
from covscreen.workflow_protocol import changed, revision, archive_branch
from . import screening_plan, screening_results, refinement_handoff, refinement_execution, refinement_results, md_selection, md_transfer

HOST_ACTIONS = {"compile_screening", "recover_screening", "approve_screening", "screening_status", "cancel_screening", "wake_screening", "release_screening", "migrate_screening_to_vs", *screening_results.RESULT_ACTIONS, *refinement_handoff.ACTIONS}
PHASES = {"DRAFT": "SCREENING_EXECUTION_DRAFT", "APPROVED": "SCREENING_EXECUTION", "ACTIVE": "SCREENING_EXECUTION",
    "NEEDS_REVIEW": "SCREENING_EXECUTION", "CANCEL_REQUESTED": "SCREENING_EXECUTION",
    "CANCELLED": "SCREENING_STOPPED", "FAILED": "SCREENING_STOPPED", "SUCCEEDED": "SCREENING_REVIEW"}


def check_reference(reference, intent, *, operation=False):
    keys = {"schema", "workflow_id", "revision", "operation_id", "intent_sha256"} | (set() if operation else {"plan_sha256"})
    if (not isinstance(reference, dict) or set(reference) != keys
        or reference.get("schema") != "library-workflow-execution-reference-v1"
        or any(reference.get(k) != intent[k] for k in ("workflow_id", "revision", "operation_id"))
        or not all(isinstance(reference.get(k), str) and re.fullmatch(r"[a-f0-9]{64}", reference[k])
                   for k in ({"intent_sha256"} if operation else {"intent_sha256", "plan_sha256"}))):
        raise ValueError("Native receipt differs from the committed scientific operation")


def observation(value, intent, handoff):
    fields = {"schema", "reference", "task_id", "task_state", "approval_recorded", "execution_state", "ledger_sequence",
              "observed_at", "observation_scope", "assessment", "remote_artifacts_rehashed"}
    if (not isinstance(value, dict) or set(value) != fields or value.get("schema") != "library-workflow-ledger-observation-v1"
        or not isinstance(value.get("task_id"), str) or not re.fullmatch(r"screen-[a-f0-9]{24}", value["task_id"])
        or value.get("task_state") not in PHASES or type(value.get("approval_recorded")) is not bool
        or value.get("execution_state") not in {"not_submitted", "submitted", "running", "completed", "failed", "outcome_unknown"}
        or type(value.get("ledger_sequence")) is not int or value["ledger_sequence"] < 1
        or type(value.get("observed_at")) is not int or value["observed_at"] < 1
        or value.get("observation_scope") != "persisted_executor_ledger" or value.get("assessment") != "not_evaluated"
        or value.get("remote_artifacts_rehashed") is not False):
        raise ValueError("Invalid native ledger observation")
    check_reference(value["reference"], intent)
    bound = handoff.get("receipt") or handoff.get("observation")
    if bound and any(value[k] != bound[k] for k in ("task_id", "reference")):
        raise ValueError("Another native task cannot replace the bound workflow task")
    if not value["approval_recorded"] and (value["task_state"] not in {"DRAFT", "CANCELLED"} or value["execution_state"] != "not_submitted"):
        raise ValueError("Native execution without approval is inconsistent")
    if (value["task_state"] == "DRAFT" and value["approval_recorded"]
        or (value["task_state"] == "SUCCEEDED") != (value["execution_state"] == "completed")):
        raise ValueError("Native task/approval/completion evidence is inconsistent")
    previous = handoff.get("observation")
    if previous:
        if value["ledger_sequence"] < previous["ledger_sequence"]:
            raise ValueError("Stale native ledger observation cannot roll back workflow state")
        if value["ledger_sequence"] == previous["ledger_sequence"] and any(value[k] != previous[k] for k in fields - {"observed_at"}):
            raise ValueError("Conflicting observation at the same ledger sequence")
    return value


def source(value):
    if not isinstance(value, dict) or set(value) != {"text", "origin"}:
        raise ValueError("Host-bound original request required")
    text, origin = value["text"], value["origin"]
    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
        raise ValueError("Invalid original user text")
    if (not isinstance(origin, dict) or set(origin) != {"user_message_id", "assistant_message_id", "part_ids", "request_sha256"}
        or not all(isinstance(origin[k], str) and origin[k] for k in ("user_message_id", "assistant_message_id"))
        or not isinstance(origin["part_ids"], list) or not origin["part_ids"]
        or not all(isinstance(item, str) and item for item in origin["part_ids"])
        or origin["request_sha256"] != hashlib.sha256(text.encode()).hexdigest()):
        raise ValueError("Original user request identity/hash mismatch")
    return value


def handle(root, name, arguments):
    from .workflow import RESEARCH, ASSETS, directory, response, view, structure, verify_preparation
    values = dict(arguments)
    token = values.pop("_workflow_handoff")
    # Bounded candidate IDs/limitations may contain multi-byte Unicode. The
    # receipt schemas still cap candidates at 100; allow their encoded envelope.
    if not isinstance(token, str) or len(token) > 500000:
        raise ValueError("Invalid workflow host envelope")
    request = decode(token)
    if (request.get("kind") != "library-screening-host-request-v1"
        or request.get("arguments") != values or name != RESEARCH
        or set(values) != {"action", "workflow_id", "expected_revision"} | {
            "release_screening": {"reason"}, "migrate_screening_to_vs": {"reason"}, "select_screening_candidates": {"selection_policy", "reason"},
            "plan_refinement": {"refinement_review", "max_points"}, "release_refinement": {"reason"},
            "select_md_candidate": {"compound_id", "input_sdf_index", "reason"}, "release_md_candidate": {"reason"}}.get(values.get("action"), set())
        or values["action"] not in HOST_ACTIONS
        or type(request.get("expires")) not in (int, float) or not 0 < request["expires"] - time.time() <= 600
        or not re.fullmatch(r"[a-f0-9]{32}", request.get("nonce", ""))
        or request.get("operation") not in {"open", "commit", "observe", "release", "result", "refinement_plan", "refinement_release", "refinement_observe", "refinement_report", "md_candidate_select", "md_candidate_release", *md_transfer.OPERATIONS}):
        raise ValueError("Workflow host purpose, scope or expiry mismatch")
    if values["action"] == "migrate_screening_to_vs":
        if (request["operation"] not in {"open", "release"} or not isinstance(values.get("reason"), str)
            or not 5 <= len(values["reason"].strip()) <= 2000):
            raise ValueError("VS migration requires its explicit purpose and reason")
    refinement = values["action"] in refinement_handoff.ACTIONS
    if refinement:
        refinement_handoff.validate_arguments(values)
    operations = {"refinement_report": {"open", "refinement_report"}, "resume_refinement_delivery": {"open", "refinement_report"},
        "select_md_candidate": {"open", "refinement_report", "md_candidate_select"},
        "release_md_candidate": {"open", "md_candidate_release"},
        "prepare_md_input": {"open", "refinement_report", *md_transfer.OPERATIONS},
        "plan_refinement": {"open", "refinement_plan"}, "release_refinement": {"open", "refinement_release"},
        "recover_refinement": {"open", "refinement_plan", "refinement_release", "refinement_observe"}}
    if (request["operation"].startswith(("refinement_", "md_candidate_", "md_transfer_")) and not refinement
        or refinement and request["operation"] not in operations.get(values["action"], {"open", "refinement_observe"})):
        raise ValueError("Refinement operation purpose mismatch")
    if values["action"] == "select_screening_candidates":
        screening_results.SelectionPolicy.model_validate(values["selection_policy"])
        if not isinstance(values["reason"], str) or not 20 <= len(values["reason"].strip()) <= 2000:
            raise ValueError("Candidate policy requires a concrete scientific review rationale")
    scope = request.get("scope")
    if (not isinstance(scope, dict) or set(scope) != {"project", "directory", "sessionID", "ledger_id"}
        or not all(isinstance(v, str) and 0 < len(v) <= 4096 for v in scope.values())
        or not Path(scope["directory"]).is_absolute() or not re.fullmatch(r"[a-f0-9]{32}", scope["ledger_id"])):
        raise ValueError("Invalid workflow host project/session scope")
    folder = directory(root, values["workflow_id"])
    with version_lock(folder, ".workflow.lock"):
        state = load(folder)
        revision(state, values["expected_revision"])
        if state["pending"] or state["stage"] not in {"SCREENING_PLAN_READY", "SCREENING_HANDOFF", "CANDIDATE_REVIEW", "SCREENING_SELECTION_EMPTY", *PHASES.values(), *refinement_handoff.PHASES}:
            raise ValueError("Scientific plan is not available for host handoff")
        if state.get("refinement_handoff") and not refinement:
            raise ValueError("The refinement operation locks its parent and reviewed subset")
        if request["operation"] == "open" and values["action"] not in view(state)["tools"].get(RESEARCH, {}).get("actions", []):
            raise ValueError("Host action is unavailable at the current workflow stage")
        handoff = state.get("screening_handoff")
        scientific = handoff is None or values["action"] in {"compile_screening", "approve_screening"} and request["operation"] == "open"
        if scientific:
            structure(root, state)
            verify_preparation(root, state, state["preparation"]["run_id"])
            intent = screening_plan.intent(root, state)
        else:
            intent = handoff.get("intent") or screening_plan.load_record(root, state, verify_inputs=False)["intent"]
        if handoff is None:
            if request["operation"] != "open" or values["action"] != "compile_screening" or state["stage"] != "SCREENING_PLAN_READY":
                raise ValueError("No committed host handoff to recover")
            handoff = {"scope": scope, "source": source(request.get("source")),
                "screening_plan": state["screening_plan"], "intent": intent, "receipt": None, "observation": None}
            state.update(screening_handoff=handoff, stage="SCREENING_HANDOFF")
            # This commit precedes returning any intent to the compiler. Even
            # an uncertain first response leaves the plan locked for recovery.
            changed(folder, state, "screening_host_bound")
        if handoff["scope"] != scope or handoff["screening_plan"] != state["screening_plan"]:
            raise ValueError("Screening handoff belongs to another host scope or plan")
        if handoff.get("intent") and intent != handoff["intent"]:
            raise ValueError("Scientific plan differs from the frozen host handoff")
        if not refinement and request.get("source") is not None and source(request["source"]) != handoff["source"]:
            raise ValueError("Screening handoff is already bound to its original user request")
        if refinement:
            if request["operation"] in {"refinement_plan", "refinement_release", "refinement_observe", "refinement_report"} and not state.get("refinement_handoff"):
                raise ValueError("Refinement must be durably opened before native planning")
            refinement_handoff.open_operation(folder, state, values, scope,
                source(request["source"]) if request.get("source") is not None else None)
            if request["operation"] == "refinement_plan":
                refinement_handoff.commit(folder, state, request.get("refinement_result"))
            if request["operation"] == "refinement_release":
                refinement_handoff.release(folder, state, request.get("refinement_closure"))
            if request["operation"] == "refinement_report":
                refinement_results.verify(state, request.get("refinement_report"), request.get("refinement_observation"))
                refinement_execution.observe(folder, state, request.get("refinement_observation"))
                refinement_results.commit(folder, state, request["refinement_report"])
            if request["operation"] == "refinement_observe":
                refinement_execution.observe(folder, state, request.get("refinement_observation"))
            if request["operation"] == "md_candidate_select":
                md_selection.select(folder, state, values, request.get("refinement_report"))
            if request["operation"] == "md_candidate_release":
                md_selection.release(folder, state, values["reason"])
            if request["operation"] == "md_transfer_open":
                md_transfer.begin(folder, state, request.get("md_choice_id"), request.get("md_native_target"))
            if request["operation"] == "md_transfer_chunk":
                md_transfer.append(folder, state, request.get("md_chunk"))
            if request["operation"] == "md_transfer_commit":
                if request.get("refinement_report") != state["refinement_handoff"].get("report_result"):
                    raise ValueError("MD freezing requires the current verified report observation")
                md_transfer.freeze(root, folder, state, request.get("md_choice_id"), request.get("md_native_target"))
        if request["operation"] == "commit":
            if handoff.get("observation") and handoff["observation"]["task_state"] != "DRAFT":
                raise ValueError("Observed native execution cannot return to a draft")
            receipt = request.get("receipt")
            if (not isinstance(receipt, dict) or set(receipt) != {"task_id", "reference", "state", "preflight_verified"}
                or not re.fullmatch(r"screen-[a-f0-9]{24}", receipt.get("task_id", ""))
                or receipt.get("state") != "DRAFT" or receipt.get("preflight_verified") is not True):
                raise ValueError("Only a preflight-verified unapproved native draft may be committed")
            check_reference(receipt.get("reference"), intent)
            if handoff["receipt"] is not None and handoff["receipt"] != receipt:
                raise ValueError("A different native task cannot replace this workflow draft")
            if handoff["receipt"] is None:
                handoff["receipt"] = receipt
                state["stage"] = "SCREENING_EXECUTION_DRAFT"
                changed(folder, state, "screening_draft_bound", {"task_id": receipt["task_id"]})
        if request["operation"] == "observe":
            value = observation(request.get("observation"), intent, handoff)
            if handoff.get("observation") != value:
                handoff["observation"] = value
                state["stage"] = screening_results.phase(handoff, PHASES[value["task_state"]])
                changed(folder, state, "screening_ledger_observed", {"task_id": value["task_id"], "ledger_sequence": value["ledger_sequence"]})
        if request["operation"] == "result":
            if values["action"] not in screening_results.RESULT_ACTIONS or values["action"] not in view(state)["tools"].get(RESEARCH, {}).get("actions", []):
                raise ValueError("Result review is unavailable at this workflow stage")
            observed = observation(request.get("observation"), intent, handoff)
            result = screening_results.verify(request.get("screening_result"), observed, handoff, values)
            if result["report_verified_at"] is not None:
                handoff["report_witness"] = result["stages"]
            chosen = result["selection"]
            if chosen:
                previous = handoff.get("selection")
                if previous and previous != chosen:
                    handoff.setdefault("selection_history", []).append(previous)
                handoff["selection"] = chosen
            handoff.update(observation=observed, screening_result=result)
            state["stage"] = screening_results.phase(handoff, "SCREENING_REVIEW")
            changed(folder, state, "screening_result_observed", {"task_id": observed["task_id"], "verification": result["verification"]})
        if request["operation"] == "release":
            closure = request.get("closure")
            if (values["action"] not in {"release_screening", "recover_screening", "migrate_screening_to_vs"}
                or not isinstance(closure, dict) or set(closure) != {"schema", "operation", "task_id", "state", "reason", "compute_submitted"}
                or closure.get("schema") != "library-workflow-operation-closure-v1" or closure.get("compute_submitted") is not False
                or closure.get("state") not in {"NO_TASK", "CANCELLED_UNAPPROVED", "CANCELLED_UNSUBMITTED_FOR_VS"}
                or not isinstance(closure.get("reason"), str) or not 5 <= len(closure["reason"].strip()) <= 2000):
                raise ValueError("A sealed unapproved native operation is required for release")
            migrating = closure["state"] == "CANCELLED_UNSUBMITTED_FOR_VS"
            if (values["action"] != "recover_screening" and migrating != (values["action"] == "migrate_screening_to_vs")
                or values["action"] == "migrate_screening_to_vs" and closure["reason"] != values["reason"].strip()):
                raise ValueError("Closure purpose or migration reason differs from the requested action")
            check_reference(closure["operation"], intent, operation=True)
            bound = handoff.get("receipt") or handoff.get("observation")
            known = handoff.get("observation") or {}
            if (closure["state"] == "NO_TASK" and (closure["task_id"] is not None or bound)
                or closure["state"] != "NO_TASK" and not re.fullmatch(r"screen-[a-f0-9]{24}", closure.get("task_id") or "")
                or bound and (closure["task_id"] != bound["task_id"] or closure["operation"]["intent_sha256"] != bound["reference"]["intent_sha256"])
                or not migrating and known.get("approval_recorded", False)
                or migrating and (not bound or known.get("task_state") != "CANCELLED" or known.get("execution_state") != "not_submitted")):
                raise ValueError("Closure conflicts with the known native task or approval")
            archive_branch(state, closure["reason"], retain=ASSETS, invalidate=("screening_plan",))
            state.setdefault("screening_handoff_history", []).append({**handoff, "closure": closure})
            state.update(screening_handoff=None, stage="VS_SCREENING_PLAN" if migrating else "SCREENING_PLAN")
            changed(folder, state, "screening_operation_migrated_to_vs" if migrating else "screening_operation_released")
        observed = handoff.get("observation")
        result = response(state, {"status": "MD_INPUT_FROZEN" if request["operation"] == "md_transfer_commit" else
            "MD_TRANSFER_OBSERVED" if request["operation"] in md_transfer.OPERATIONS else
            "MD_CANDIDATE_SELECTED" if request["operation"] == "md_candidate_select" else
            "MD_CANDIDATE_RELEASED" if request["operation"] == "md_candidate_release" else
            "REFINEMENT_REPORT_OBSERVED" if request["operation"] == "refinement_report" else
            "REFINEMENT_LEDGER_OBSERVED" if request["operation"] == "refinement_observe" else
            "REFINEMENT_PLANNING_RELEASED" if request["operation"] == "refinement_release" else
            "REFINEMENT_PLAN_OBSERVED" if request["operation"] == "refinement_plan" else
            "REFINEMENT_HOST_BOUND" if refinement else
            ("SCREENING_OPERATION_MIGRATED_TO_VS" if state["stage"] == "VS_SCREENING_PLAN" else "SCREENING_OPERATION_RELEASED") if request["operation"] == "release" else
            "SCREENING_RESULT_OBSERVED" if request["operation"] == "result" else
            "SCREENING_LEDGER_OBSERVED" if observed else "SCREENING_DRAFT_BOUND" if handoff["receipt"] else "SCREENING_HOST_BOUND",
            "task_id": (observed or handoff.get("receipt") or {}).get("task_id"),
            **({"calculation_submitted": False} if not observed else {}),
            **({"calculation_submitted": False, "new_approval_required": True,
                "historical_approval_recorded": bool(observed and observed["approval_recorded"])}
                if request["operation"] == "release" and state["stage"] == "VS_SCREENING_PLAN" else {}),
            "compute_submitted_by_adapter": False, "approval_recorded":
                ((state.get("refinement_handoff") or {}).get("execution_observation") or {}).get("approval_recorded", False)
                if refinement else False if request["operation"] == "release" else observed["approval_recorded"] if observed else False})
        # Internal exchange only. The host authenticates this response and
        # removes the envelope before returning the result to model/tool traces.
        result["_host_handoff"] = signed({"kind": "library-screening-host-response-v1",
            "request_sha256": hashlib.sha256(token.encode()).hexdigest(), "scope": scope,
            "public_result": dict(result), "screening_plan": handoff["screening_plan"],
            "intent": intent, "source": handoff["source"], "receipt": handoff["receipt"],
            "observation": observed, "closure": request.get("closure") if request["operation"] == "release" else None,
            "screening_result": handoff.get("screening_result"), "selection_binding": screening_results.binding(handoff.get("selection")),
            "refinement_handoff": state.get("refinement_handoff"),
            "md_candidate": state.get("md_candidate"),
            "md_transfer": state.get("md_transfer"), "md_input": state.get("md_input"),
            "refinement_closure": request.get("refinement_closure") if request["operation"] == "refinement_release" else None})
        return result
