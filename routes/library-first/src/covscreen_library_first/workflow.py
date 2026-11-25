"""Durable Library-first research navigation over the existing scientific APIs.

The route binds artifacts; native approval and JobManager remain the only
execution authority. Screening/MD handoff is a separate, explicit boundary.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re

from covscreen import tool_api as api
from covscreen.ligand_parent_evidence import version_lock
from covscreen.workflow_store import load, save
from covscreen.workflow_protocol import archive_branch, begin_operation, changed, envelope, revision
from covscreen.target_identity import target_plan
from covscreen.structure_discovery import load_target_discovery
from covscreen.receptor_input import load_receptor_preparation_input
from covscreen.jobs import sha256_file
from covscreen.metric_contracts import workflow_metric_context
from covscreen import library_vs_prescreen_execution as vs_prescreen
from covscreen import library_vs_input as vs_input
from covscreen import library_vs_execution as vs_execution
from covscreen import library_lo_input as lo_input
from covscreen import library_lo_execution as lo_execution
from . import refinement_handoff, refinement_execution, md_selection, md_transfer, md_execution
from . import historical_md
from .workflow_actions import FIELDS

ENTRY = "covalent_library_workflow"
RESEARCH = "covalent_library_research"
RECEPTOR = "covalent_library_receptor"
INTERFACE = {
    "schema": "research-workflow-interface-v1", "route": "library-first",
    "protocol": "library-workflow-v1", "id_pattern": "^library-flow-[a-f0-9]{24}$",
    "entry": ENTRY, "tools": [ENTRY, RESEARCH, RECEPTOR],
    "start_fields": ["action", "request_id", "target", "taxon", "workflow_id"],
    "legacy_names": ["covalent_plan_library_cascade", "covalent_next_library_action"],
    "legacy_prefixes": ["covalent_library_first_"],
    "refreshable": {RESEARCH: ["read_target", "read_structures", "read_structure", "read_screening_receptor", "screening_profiles", "read_screening_inputs", "read_screening_plan"], RECEPTOR: ["status"]},
}
ASSETS = ("target_identity", "site_selection", "discovery", "receptor_input", "receptor_plan", "preparation", "receptor_context", "screening_receptor", "screening_inputs", "screening_plan")


def directory(root: Path, identifier: str):
    if not isinstance(identifier, str) or not re.fullmatch(r"library-flow-[a-f0-9]{24}", identifier):
        raise ValueError("Invalid Library-first workflow ID")
    folder = root / "state/library-workflows" / identifier
    if any(p.is_symlink() for p in (root / "state", folder.parent, folder)):
        raise ValueError("Workflow directory cannot be a symbolic link")
    return folder


def local_operation(pending):
    """Only explicitly local operations can be ruled out as uncertain submissions."""
    if not pending or pending.get("submission"):
        return False
    return pending.get("action") in {
        RESEARCH: {"resolve_target", "discover_structures", "acquire_structure", "review_receptor",
                   "prepare_screening_inputs", "prepare_screening_plan", "attach_historical_md_source",
                   "prepare_historical_md_candidate"},
        RECEPTOR: {"plan", "assess_context"},
    }.get(pending.get("tool"), set())


def view(state):
    stage = state["stage"]
    handoff = state.get("screening_handoff") or {}
    result = handoff.get("screening_result")
    refinement = state.get("refinement_handoff") or {}
    refinement_observed = refinement.get("execution_observation") or {}
    review = ["read_screening_plan", "screening_status", "screening_report"]
    if handoff.get("selection"):
        review.append("read_screening_candidates")
    # A missing subset must not deadlock research: an independently verified
    # core report still allows an explicit new policy, never automatic fallback.
    if result and result["report_verified_at"] is not None and result["verification"] in {"verified", "selection_unavailable"}:
        review.append("select_screening_candidates")
    if stage == "CANDIDATE_REVIEW" and result and result["verification"] == "verified" and result.get("selection") and result["selection"]["selected_count"]:
        review.append("plan_refinement")
    navigation = ["status", "suspend"]
    prescreen = state.get('vs_prescreen') or {}
    vs = state.get('vs_execution') or {}
    vs_closed = (vs.get('job_state') in vs_execution.TERMINAL and vs.get('job_identity_verified')
        and not vs.get('evidence_error') and not state.get('md_input') and not state.get('md_execution')
        and (vs['job_state'] != 'COMPLETED' or bool(vs.get('evaluation_summary'))))
    prescreen_closed = (prescreen.get('job_state') in vs_prescreen.TERMINAL and prescreen.get('job_identity_verified')
                        and not prescreen.get('evidence_error') and (not vs or vs_closed))
    lo = state.get('lo_execution') or {}
    lo_closed = (lo.get('job_state') in lo_execution.TERMINAL and lo.get('job_identity_verified')
        and not lo.get('evidence_error') and not state.get('md_input') and not state.get('md_execution')
        and ((lo.get('job_state'), lo.get('return_code')) not in {('COMPLETED', 0), ('FAILED', 2)}
             or bool(lo.get('evaluation_summary'))))
    if (not state["pending"] and stage != "RECEPTOR_PREPARATION" and not state.get("screening_handoff")
        and (not prescreen or prescreen_closed)
        and (not lo or lo_closed)
        and not (state.get("historical_md_source") and (state.get("md_input") or state.get("md_execution")))):
        navigation.append("reconsider")
    if state["pending"]:
        navigation.append("reconcile")
    if state["branches"]:
        navigation.append("inspect_branch")
    if stage == "TARGET_REVIEW" and state["target_identity"] and not state["pending"]:
        navigation.append("select_site")
    actions = {
        "TARGET_REVIEW": ["read_target"] if state["target_identity"] else ["resolve_target"],
        "STRUCTURE_REVIEW": ["read_target", "discover_structures"],
        "STRUCTURE_SELECTION": ["read_target", "read_structures", "acquire_structure"],
        "RECEPTOR_INPUT_READY": ["read_target", "read_structures", "read_structure"],
        "RECEPTOR_PLAN_READY": ["read_target", "read_structures", "read_structure"],
        "RECEPTOR_REVIEW": ["read_structure"], "SCREENING_PLAN": ["read_structure", "review_receptor"]
            + (["read_screening_receptor", "screening_profiles", "prepare_screening_inputs"] if state.get("screening_receptor") else [])
            + (["read_screening_inputs", "prepare_screening_plan"] if state.get("screening_inputs") else []),
        "SCREENING_PLAN_READY": ["read_structure", "read_screening_receptor", "read_screening_inputs", "read_screening_plan",
            "prepare_screening_plan", "review_receptor", "screening_profiles", "prepare_screening_inputs", "compile_screening"],
        "SCREENING_HANDOFF": ["read_screening_plan", "recover_screening", "release_screening"],
        "SCREENING_EXECUTION_DRAFT": ["read_screening_plan", "recover_screening", "approve_screening", "screening_status", "release_screening"],
        "SCREENING_EXECUTION": ["read_screening_plan", "screening_status", "cancel_screening", "wake_screening"],
        "SCREENING_STOPPED": ["read_screening_plan", "screening_status"] +
            (["release_screening"] if not (handoff.get("observation") or {}).get("approval_recorded", True) else []) +
            (["migrate_screening_to_vs", "recover_screening"] if (handoff.get("observation") or {}).get("task_state") == "CANCELLED"
                and (handoff.get("observation") or {}).get("execution_state") == "not_submitted" else []),
        "VS_SCREENING_PLAN": ["read_structure", "read_screening_receptor", "read_screening_inputs"],
        "SCREENING_REVIEW": review, "CANDIDATE_REVIEW": review, "SCREENING_SELECTION_EMPTY": review,
        **{phase: ["read_screening_plan", "recover_refinement", "release_refinement"] for phase in refinement_handoff.PHASES},
        "REFINEMENT_PLAN_READY": ["read_screening_plan", "recover_refinement", "refinement_status", "approve_refinement", "release_refinement"],
        "REFINEMENT_PLAN_BLOCKED": ["read_screening_plan", "recover_refinement", "release_refinement"] + (["refinement_status"] if refinement.get("draft_identity") else []),
        "REFINEMENT_APPROVAL_PENDING": ["recover_refinement", "refinement_status", "cancel_refinement"],
        "REFINEMENT_EXECUTION": ["recover_refinement", "refinement_status", "cancel_refinement", "wake_refinement", "refinement_report"],
        "REFINEMENT_STOPPED": ["recover_refinement", "refinement_status", "refinement_report"] + ([] if refinement_observed.get("approval_recorded") else ["release_refinement"]),
        "REFINEMENT_REVIEW": ["recover_refinement", "refinement_status", "refinement_report"],
        "REFINEMENT_CANDIDATE_REVIEW": ["recover_refinement", "refinement_status", "refinement_report"],
    }.get(stage, []) if not state["pending"] else []
    actions.extend(vs_prescreen.actions(state))
    actions.extend(vs_input.actions(state))
    actions.extend(vs_execution.actions(state))
    actions.extend(lo_input.actions(state))
    actions.extend(lo_execution.actions(state))
    if not state['pending'] and stage == 'STRUCTURE_REVIEW' and not state.get('historical_md_source'):
        actions.append('attach_historical_md_source')
    if not state['pending'] and stage == 'HISTORICAL_MD_SOURCE_REVIEW':
        actions = ['read_target', 'read_historical_md_source']
        if state['historical_md_source'].get('verification') == 'source_chain_verified':
            actions.append('plan_md_admission' if state.get('md_input') else 'prepare_historical_md_candidate')
    delivery = (refinement.get("report_result") or {}).get("delivery")
    if (not state["pending"] and stage == "REFINEMENT_CANDIDATE_REVIEW"
        and refinement_observed.get("execution_state") == "completed" and delivery
        and delivery["state"] in {"NOT_STARTED", "WAITING", "ACTIVE", "PAUSED"}):
        actions.append("resume_refinement_delivery")
    if not state["pending"] and stage in {"REFINEMENT_REVIEW", "REFINEMENT_CANDIDATE_REVIEW"}:
        if state.get("md_candidate"):
            if md_selection.public(state)["state"] in {"selected_asset_handoff_pending", "selected_input_frozen"}:
                actions.append("prepare_md_input")
            if not any(state.get(key) for key in ("md_transfer", "md_input", "md_execution")):
                actions.append("release_md_candidate")
        elif stage == "REFINEMENT_CANDIDATE_REVIEW" and md_selection.available(state):
            actions.append("select_md_candidate")
        if state.get("md_input") and not state.get("md_execution"):
            actions.append("plan_md_admission")
    if not state["pending"] and state.get("md_execution"):
        actions = md_execution.actions(state)
    receptor = {"RECEPTOR_INPUT_READY": ["plan"], "RECEPTOR_PLAN_READY": ["plan", "start"],
                "RECEPTOR_PREPARATION": ["status"], "RECEPTOR_REVIEW": ["status", "assess_context"],
                "RECEPTOR_FAILED": ["status"], "SCREENING_PLAN": ["status", "assess_context"],
                "SCREENING_PLAN_READY": ["status", "assess_context"]}.get(stage, [])
    tools = {ENTRY: {"actions": navigation, "fields": ["action", "workflow_id", "expected_revision", "reason",
        "return_to", "branch_index", "site"]}}
    for tool, allowed in ((RESEARCH, actions), (RECEPTOR, receptor if not state["pending"] else [])):
        if allowed:
            tools[tool] = {"actions": allowed, "fields": sorted({"action", "workflow_id", "expected_revision"}
                | {field for action in allowed for field in FIELDS[action]})}
    for action in actions:
        if action.startswith("plan_md_"):
            tools[RESEARCH]["field_constraints"] = {"resource_budget": md_execution.budget_constraint(action.removeprefix("plan_md_"))}
    if 'review_lo_environment' in actions or 'review_vs_environment' in actions:
        from covscreen.receptor_environment_review import REVIEW_SCHEMA
        schema = deepcopy(REVIEW_SCHEMA)
        if 'review_vs_environment' in actions:
            for item in schema['properties'].values():
                item['properties']['decision']['enum'] = ['suitable_for_exploratory_vs', 'requires_revision', 'evidence_insufficient']
        tools[RESEARCH]['field_constraints'] = {'environment_review': schema}
    return {"protocol": INTERFACE["protocol"], "workflow_id": state["workflow_id"],
            "revision": state["revision"], "stage": stage, "tools": tools}


def response(state, result=None):
    phase = state["stage"]
    blockers = []
    if state["pending"]:
        blockers.append({"code": "operation_reconciliation_required", "scope": "workflow_operation",
                         "reason": "An operation has no committed workflow receipt; never replay start.", "recoverable": True})
    historical = state.get('historical_md_source')
    if historical and historical.get('verification') != 'source_chain_verified':
        blockers.append({'code': 'evidence_rejected', 'scope': 'historical_md_source',
                         'reason': historical.get('error', 'Historical input requires verification'), 'recoverable': True})
    if phase == 'HISTORICAL_MD_SOURCE_REVIEW' and historical and historical.get('verification') == 'source_chain_verified':
        blockers.append({'code': 'md_admission_plan_required' if state.get('md_input') else 'md_input_preparation_required',
            'scope': 'historical_md_source', 'reason': 'Historical input is retained; native graph admission requires a new independently approved plan.',
            'recoverable': True})
    if phase in {"SCREENING_PLAN", "SCREENING_PLAN_READY"}:
        blockers.append({"code": "scientific_plan_required" if phase == "SCREENING_PLAN" else "host_compilation_required", "scope": "workflow_screening_handoff",
            "reason": "Commit a scientific plan and use the authenticated host adapter to prepare its native draft.", "recoverable": True})
    if phase == "SCREENING_HANDOFF":
        blockers.append({"code": "host_handoff_recovery_required", "scope": "workflow_screening_handoff",
            "reason": "The scientific plan is locked to its original host request; recover the same operation.", "recoverable": True})
    if phase == "VS_SCREENING_PLAN":
        blockers.append({"code": "vs_screening_plan_required", "scope": "library_vs_migration",
            "reason": "Review the retained frozen prefix and prepare a separate native CPU prescreen plan; historical approval grants no permission for this plan.",
            "recoverable": True})
    if phase in vs_prescreen.STAGES:
        blockers.append({'code': 'native_approval_required' if phase == 'VS_PRESCREEN_PLAN_READY' else 'vs_prescreen_evidence_required',
            'scope': 'library_vs_prescreen', 'reason': 'Approve the exact new CPU prescreen plan.' if phase == 'VS_PRESCREEN_PLAN_READY'
                else 'Observe the original task and verify its candidate outputs before VS input preparation; execution is not scientific acceptance.',
            'recoverable': True})
    if phase in vs_input.STAGES:
        blockers.append({'code': 'native_approval_required' if phase == 'VS_PLAN_READY' else 'vs_environment_review_required',
            'scope': 'library_covdock_vs', 'reason': state.get('vs_input_error') or
                ('The exact VS plan requires separate native approval and a release with VS execution enabled.' if phase == 'VS_PLAN_READY'
                 else 'Review the retained receptor environment and exact prescreen candidates, then check the VS runtime.'),
            'recoverable': True})
    if phase in vs_execution.STAGES:
        blockers.append({'code': 'vs_product_assessment_required' if phase == 'VS_EXECUTION_REVIEW' else 'vs_execution_pending',
            'scope': 'library_covdock_vs', 'reason': (state.get('vs_execution') or {}).get('evidence_error') or
                'Observe the same registered VS task; evaluate_vs re-reads raw products and report_vs saves the scientific review. Completion or geometry checks do not admit MD.',
            'recoverable': True})
    if phase in lo_input.STAGES | lo_execution.STAGES:
        error = state.get('lo_input_error') or (state.get('lo_execution') or {}).get('evidence_error')
        blockers.append({'code': 'evidence_rejected' if error else 'native_approval_required' if phase == 'LO_PLAN_READY' else 'lo_review_required',
            'scope': 'library_covdock_lo', 'reason': error or 'LO has its own candidate subset, environment review, native approval and result checks; MD admission remains separate.',
            'recoverable': True})
    if phase == "SCREENING_EXECUTION_DRAFT":
        blockers.append({"code": "native_approval_required", "scope": "workflow_screening_approval",
            "reason": "The bound native draft needs exact user approval; planning is not permission.", "recoverable": True})
    if phase == "SCREENING_REVIEW":
        blockers.append({"code": "screening_result_review_required", "scope": "workflow_screening_report_handoff",
            "reason": "Read the current bound report and choose an explicit candidate policy with scientific rationale.", "recoverable": True})
    if phase == "CANDIDATE_REVIEW":
        blockers.append({"code": "refinement_review_required", "scope": "workflow_refinement_plan_handoff",
            "reason": "Review the exact candidate subset and propose a separate refinement budget before native planning.", "recoverable": True})
    if phase in refinement_handoff.PHASES:
        code, reason = {
            "REFINEMENT_PLAN_READY": ("native_approval_required", "The exact two-engine refinement plan needs independent native approval; planning is not permission."),
            "REFINEMENT_APPROVAL_PENDING": ("native_approval_pending", "Recover or cancel the existing native confirmation; never create a duplicate."),
            "REFINEMENT_EXECUTION": ("refinement_execution_pending", "Observe the original native stages; cancellation intent does not prove remote termination."),
            "REFINEMENT_STOPPED": ("refinement_stopped", "Retain terminal evidence; approved executions cannot be released as unsubmitted planning."),
            "REFINEMENT_REVIEW": ("fresh_refinement_report_required", "Read the exact native report and freshly verify both engine artifacts before candidate review."),
            "REFINEMENT_CANDIDATE_REVIEW": ("md_admission_plan_required", "Plan the existing native graph admission engine for the frozen pose; independent native approval is required before execution.") if state.get("md_input") else
                ("md_input_preparation_required", "Select an explicit eligible candidate, then prepare_md_input transfers its exact files and freezes its pose; no MD compute is submitted."),
        }.get(phase, ("refinement_planning_reconciliation_required", "Recover the same plan or explicitly seal unapproved planning before revising it."))
        blockers.append({"code": code, "scope": "workflow_refinement", "reason": reason, "recoverable": code != "capability_not_implemented"})
    if phase == "SCREENING_SELECTION_EMPTY":
        blockers.append({"code": "no_candidates_meet_policy", "scope": "candidate_selection",
            "reason": "The explicit policy selected zero candidates; review exclusions or revise the policy without filling unused quotas.", "recoverable": True})
    md = md_execution.public(state)
    if md:
        code, reason = ("native_approval_required", "This MD stage requires independent approval of the exact frozen plan.") if phase.endswith("_PLAN_READY") else (
            ("md_parameterization_plan_required", "Native graph evidence is verified; plan the existing parameterization engine with independent approval.")
            if md["stage"] == "native_graph_admission" else
            ("md_system_preparation_plan_required", "Parameterization evidence is verified; plan the existing system preparation engine with independent approval.")
            if md["stage"] == "parameterization" else
            ("md_short_qc_plan_required", "System preparation receipts are verified; plan the bounded short QC with independent approval.")
            if md["stage"] == "system_preparation" else
            ("md_equilibration_plan_required", "Short QC receipts are verified; plan registered equilibration with independent approval.")
            if md["stage"] == "short_qc" else
            ("capability_not_implemented", "Equilibration receipts support production review; production and analysis workflow integration remain pending.")
            if md["production_review_ready"] else
            ("equilibration_not_admitted", "The equilibration protocol completed but did not meet the existing production admission checks; review diagnostic evidence.")) if md["verification"] == "verified" else (
            "md_stage_evidence_required", "Observe the existing job and verify its exact outputs; never resubmit an uncertain run.")
        blockers.append({"code": code, "scope": md["stage"], "reason": reason,
                         "recoverable": code != "capability_not_implemented"})
        if md["verification"] == "evidence_rejected":
            blockers[-1].update(code="md_stage_evidence_rejected",
                reason="The original MD job's identity, inputs or outputs failed verification: "
                    + (md["evidence_error"] or "Detailed diagnostics have not been recorded; read the original stage status.")
                    + " Retain the original task and review its evidence before any new plan.")
        elif md["job_state"] in {"FAILED", "TIMED_OUT", "CANCELLED"}:
            blockers[-1].update(code="md_stage_failed",
                reason="The original MD job ended as " + md["job_state"] + ": "
                    + (md["failure_reason"] or md["message"] or "No failure detail recorded; read the original stage status.")
                    + " Review the terminal result and retained artifacts. retry_md_stage prepares a separately approved new attempt; it never resubmits automatically."
                    + (" Last retry planning error: " + md["retry_plan_error"] if md["retry_plan_error"] else ""))
    prep = state["preparation"]
    execution = "outcome_unknown" if state["pending"] and not local_operation(state["pending"]) else {
        "COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "failed", "TIMED_OUT": "failed",
        "RUNNING": "running", "QUEUED": "submitted", "PLANNED": "not_submitted",
    }.get(prep.get("state"), "outcome_unknown") if prep else "not_submitted"
    artifacts = [{"kind": name, "id": state[name]["id"], "sha256": state[name]["sha256"], "hash_scope": "manifest"}
                 for name in ("target_identity", "discovery", "receptor_input", "receptor_context", "screening_receptor", "screening_inputs", "screening_plan") if state.get(name)]
    if state.get("md_input"):
        artifacts.append({"kind": "candidate_pose_input", "id": state["md_input"]["artifact_id"],
            "sha256": state["md_input"]["manifest_sha256"], "hash_scope": "manifest"})
    if (state.get('vs_execution') or {}).get('evaluation_summary'):
        reviewed = state['vs_execution']['evaluation_artifact']
        artifacts.append({'kind': 'library_vs_geometry_review', 'id': reviewed['artifact_id'],
                          'sha256': reviewed['manifest_sha256'], 'hash_scope': 'artifact'})
    if state.get('lo_input') and not state.get('lo_input_error'):
        ref = state['lo_input']
        artifacts.append({'kind': 'library_lo_input', 'id': ref['artifact_id'],
                          'sha256': ref['manifest_sha256'], 'hash_scope': 'manifest'})
    if (state.get('lo_execution') or {}).get('evaluation_summary'):
        ref = state['lo_execution']['evaluation_artifact']
        artifacts.append({'kind': 'library_lo_measurement_review', 'id': ref['artifact_id'],
                          'sha256': ref['manifest_sha256'], 'hash_scope': 'artifact'})
    handoff = state.get("screening_handoff")
    task = handoff.get("receipt") if handoff else None
    observed = handoff.get("observation") if handoff else None
    refinement_observed = (state.get("refinement_handoff") or {}).get("execution_observation")
    output = envelope(route="library-first", workflow=view(state),
        metric_context=workflow_metric_context("library-first", phase),
        execution=({'scope': 'registered_library_covdock_lo', 'state': 'outcome_unknown' if state['pending'] or state['lo_execution'].get('evidence_error') else
            {'QUEUED': 'submitted', 'RUNNING': 'running', 'COMPLETED': 'completed', 'FAILED': 'failed',
             'CANCELLED': 'failed', 'TIMED_OUT': 'failed'}.get(state['lo_execution'].get('job_state'), 'outcome_unknown'),
            'task_ids': [state['lo_execution']['run_id']] if state['lo_execution'].get('run_id') else []} if phase in lo_execution.STAGES else
        {'scope': 'library_covdock_lo_planning', 'state': 'not_submitted', 'task_ids': []} if phase in lo_input.STAGES else
        {'scope': 'registered_library_covdock_vs', 'state': 'outcome_unknown' if state['pending'] or state['vs_execution'].get('evidence_error') else
            {'QUEUED': 'submitted', 'RUNNING': 'running', 'COMPLETED': 'completed', 'FAILED': 'failed',
             'CANCELLED': 'failed', 'TIMED_OUT': 'failed'}.get(state['vs_execution'].get('job_state'), 'outcome_unknown'),
            'task_ids': [state['vs_execution']['run_id']] if state['vs_execution'].get('run_id') else []} if phase in vs_execution.STAGES else
        {'scope': 'library_covdock_vs_planning', 'state': 'outcome_unknown' if state['pending'] else 'not_submitted',
         'task_ids': []} if phase in vs_input.STAGES else
        {'scope': 'registered_library_vs_prescreen', 'state': 'outcome_unknown' if state['pending'] or state['vs_prescreen'].get('evidence_error') else
            {'PLANNED': 'not_submitted', 'QUEUED': 'submitted', 'RUNNING': 'running', 'COMPLETED': 'completed',
             'FAILED': 'failed', 'CANCELLED': 'failed', 'TIMED_OUT': 'failed'}.get(state['vs_prescreen']['job_state'], 'outcome_unknown'),
            'task_ids': [state['vs_prescreen']['run_id']] if state['vs_prescreen'].get('run_id') else []} if phase in vs_prescreen.STAGES else
        {"scope": "library_vs_screening_planning", "state": "not_submitted", "task_ids": []} if phase == "VS_SCREENING_PLAN" else
        {"scope": "registered_" + md["stage"], "state": "outcome_unknown" if state["pending"] else
            {"PLANNED": "not_submitted", "QUEUED": "submitted", "RUNNING": "running", "COMPLETED": "completed",
             "FAILED": "failed", "CANCELLED": "failed", "TIMED_OUT": "failed"}.get(md["job_state"], "outcome_unknown"),
            "task_ids": [md["run_id"]] if md["run_id"] else []} if md else
        {"scope": "registered_library_refinement" if refinement_observed else "refinement_draft_preparation" if state.get("refinement_handoff") else "registered_library_screening" if observed else "screening_draft_preparation" if handoff else "registered_receptor_preparation",
                   "state": refinement_observed["execution_state"] if refinement_observed else "not_submitted" if state.get("refinement_handoff") else observed["execution_state"] if observed else "not_submitted" if handoff else execution,
                   "task_ids": [s["task_id"] for s in refinement_observed["stages"]] if refinement_observed else [] if state.get("refinement_handoff") else [observed["task_id"]] if observed else [task["task_id"]] if task else [] if handoff else [prep["run_id"]] if prep else []}),
        refinement_observation={k: v for k, v in refinement_observed.items() if k != "reference"} if refinement_observed else None,
        refinement_plan=refinement_handoff.public(state),
        md_candidate=md_selection.public(state),
        historical_md_source=deepcopy(state.get('historical_md_source')),
        vs_prescreen=vs_prescreen.public(state),
        vs_input=deepcopy(state.get('vs_input')), vs_environment_review=deepcopy(state.get('vs_environment_review')),
        vs_runtime=deepcopy(state.get('vs_runtime')), vs_input_error=state.get('vs_input_error'),
        vs_execution=vs_execution.public(state),
        lo_input=deepcopy(state.get('lo_input')), lo_runtime=deepcopy(state.get('lo_runtime')),
        lo_environment_review=deepcopy(state.get('lo_environment_review')), lo_input_error=state.get('lo_input_error'),
        lo_execution=lo_execution.public(state),
        md_transfer=md_transfer.public(state), md_input=deepcopy(state.get("md_input")), md_execution=md,
        refinement_result={k: v for k, v in state["refinement_handoff"]["report_result"].items() if k != "reference"}
            if state.get("refinement_handoff") and state["refinement_handoff"].get("report_result") else None,
        screening_observation={k: observed[k] for k in ("task_state", "ledger_sequence", "observed_at", "observation_scope", "remote_artifacts_rehashed")} if observed else None,
        screening_result={k: v for k, v in handoff["screening_result"].items() if k != "reference"} if handoff and handoff.get("screening_result") else None,
        assessment=({'scope': 'library_covdock_lo', 'state': 'evidence_insufficient' if state.get('lo_input_error') or
            (state.get('lo_execution') or {}).get('evidence_error') else 'review_required',
            'reason': 'Independent LO review and separate MD admission are required; task completion does not admit MD.'}
            if phase in lo_input.STAGES | lo_execution.STAGES else vs_execution.assessment(state) if phase in vs_execution.STAGES else
        {"scope": "drug_design_validation", "state": "not_evaluated",
                    "reason": "Sequence identity, coordinates and receptor preparation do not establish inhibition or cysteine reactivity."}),
        artifacts=artifacts, blockers=blockers, target=state["target"], site_selection=state["site_selection"],
        branches=state["branches"], pending=state["pending"], result=result)
    if 'next_action' not in output:
        hint = _stage_next_action(phase)
        if hint:
            output['next_action'] = hint
    return output


def _stage_next_action(phase: str) -> str | None:
    if phase == 'TARGET_REVIEW':
        return ("Target identity verifies sequence and canonical cysteine count only; do not infer domain or pKa. "
                "select_site records an exploratory hypothesis; structural mapping follows afterwards.")
    if phase in {'VS_EXECUTION_RUNNING', 'LO_EXECUTION_RUNNING', 'VS_PRESCREEN_EXECUTION', 'SCREENING_EXECUTION', 'RECEPTOR_PREPARATION'}:
        return ("A local QUEUED receipt is registration only, not confirmed GPU allocation; zero-GPU plans do not require GPU submission. "
                "Query status once and end the turn while waiting.")
    if phase in {'VS_EXECUTION_REVIEW', 'LO_EXECUTION_REVIEW', 'SCREENING_REVIEW', 'CANDIDATE_REVIEW'}:
        return ("Preserve denominators, exclusions, and missing/null values as unmeasured (never zero). "
                "Strict CovDock v3 local strain uses r_strain_Local_Strain_Energy; structural QC pass does not establish activity.")
    return None


def identity(root, state):
    asset = state["target_identity"]
    if not asset:
        raise ValueError("Resolve target identity first")
    data = api._load_target_artifact(root, asset["id"], asset["sha256"])
    if (data["plan"]["gene"], data["plan"]["taxon"]) != (state["target"]["target"], state["target"]["taxon"]):
        raise ValueError("Target evidence belongs to another gene/species")
    return data


def discovery(root, state):
    target = identity(root, state)
    asset = state["discovery"]
    path = root / "state/target_structures" / asset["id"].removeprefix("structures-")
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Discovery path cannot be a symbolic link")
    data = load_target_discovery(path, asset["sha256"])
    expected = {"target_artifact_id": state["target_identity"]["id"],
        "target_manifest_sha256": state["target_identity"]["sha256"],
        "gene": state["target"]["target"], "taxon": state["target"]["taxon"],
        "uniprot": target["identity"]["unique_verified_target"]}
    if any(data["plan"].get(k) != v for k, v in expected.items()):
        raise ValueError("Structure discovery belongs to another target identity")
    return data


def structure(root, state):
    parent = discovery(root, state)
    asset = state["receptor_input"]
    data = load_receptor_preparation_input(root, asset["id"], asset["sha256"])
    acquired = data["acquisition"]
    expected = {"target_artifact_id": state["target_identity"]["id"],
        "target_manifest_sha256": state["target_identity"]["sha256"],
        "discovery_artifact_id": state["discovery"]["id"], "discovery_manifest_sha256": state["discovery"]["sha256"]}
    if (any(acquired["parents"].get(k) != v for k, v in expected.items())
        or acquired["acquisition"]["plan"]["entry"] not in parent["discovery"]["entry_ids"]
        or acquired["acquisition"]["plan"]["cys"] != int(state["site_selection"]["site"][1:])):
        raise ValueError("Receptor input belongs to another selected structure/site or target")
    return acquired


def reference(value, prefix):
    identifier = value.get("artifact_id")
    checksum = value.get("manifest_sha256")
    if not isinstance(identifier, str) or not re.fullmatch(re.escape(prefix) + r"[a-f0-9]{32}", identifier):
        raise ValueError("Invalid scientific artifact ID")
    if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise ValueError("Invalid scientific manifest hash")
    return {"id": identifier, "sha256": checksum}


def start(root, *, request_id, target, taxon):
    target_plan(target, taxon)
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", request_id):
        raise ValueError("Invalid host request identity")
    identifier = "library-flow-" + sha256(request_id.encode()).hexdigest()[:24]
    folder = directory(root, identifier)
    folder.mkdir(parents=True, exist_ok=True)
    with version_lock(folder, ".workflow.lock"):
        target = {"target": target, "taxon": taxon}
        if (folder / "state.json").exists():
            state = load(folder)
            if state["target"] != target:
                raise ValueError("Request identity already binds another scientific target")
            return response(state)
        state = {"workflow_id": identifier, "revision": 0, "target": target, "stage": "TARGET_REVIEW",
                 "request_id": request_id, "pending": None, "events": [], "branches": [], **dict.fromkeys(ASSETS)}
        changed(folder, state, "start")
        return response(state)


def navigate(root, action, **args):
    if action == "start":
        return start(root, **args)
    allowed = {"workflow_id"} if action in {"status", "resume"} else {"workflow_id", "expected_revision"} | {
        "select_site": {"site", "reason"}, "reconsider": {"return_to", "reason"}, "suspend": {"reason"},
        "inspect_branch": {"branch_index"}, "reconcile": set()}.get(action, set())
    if action not in {"status", "resume", "suspend", "select_site", "reconsider", "inspect_branch", "reconcile"} or set(args) != allowed:
        raise ValueError("Invalid workflow navigation arguments")
    folder = directory(root, args["workflow_id"])
    with version_lock(folder, ".workflow.lock"):
        state = load(folder)
        if action in {"status", "resume"}:
            if state.get('vs_prescreen') and not state['pending']:
                before = deepcopy(state)
                vs_prescreen.refresh(root, state)
                vs_input.refresh(root, state)
                vs_execution.refresh(root, state)
                lo_input.refresh(root, state)
                lo_execution.refresh(root, state)
                if before != state:
                    changed(folder, state, 'vs_prescreen_revalidated')
            if state.get('historical_md_source') and not state['pending']:
                before = deepcopy(state['historical_md_source'])
                historical_md.refresh(root, state)
                if before != state['historical_md_source']:
                    changed(folder, state, 'historical_md_source_refreshed')
            return response(state)
        revision(state, args["expected_revision"])
        if action == 'reconsider' and state.get('vs_prescreen'):
            # A cached terminal label cannot release navigation after source or
            # task evidence changed. Recheck under the same workflow lock.
            before = deepcopy(state)
            vs_prescreen.refresh(root, state)
            vs_execution.refresh(root, state)
            lo_input.refresh(root, state)
            lo_execution.refresh(root, state)
            if before != state:
                changed(folder, state, 'vs_prescreen_revalidated')
        if action not in view(state)["tools"][ENTRY]["actions"]:
            raise ValueError("Navigation unavailable at this stage")
        if action == "inspect_branch":
            index = args["branch_index"]
            if type(index) is not int or not 0 <= index < len(state["branches"]):
                raise ValueError("Invalid branch index")
            return response(state, {"historical_branch": state["branches"][index], "active_handoff_restored": False})
        if action == "reconcile":
            pending = state["pending"]
            if pending.get('action') == 'start_lo':
                found = lo_execution.reconcile(root, state)
                changed(folder, state, 'library_lo_reconciled')
                return response(state, found)
            if pending.get('action') == 'start_vs_prescreen':
                found = vs_prescreen.reconcile(root, state)
                changed(folder, state, 'vs_prescreen_reconciled')
                return response(state, found)
            if pending.get('action') == 'start_vs':
                found = vs_execution.reconcile(root, state)
                changed(folder, state, 'library_vs_reconciled')
                return response(state, found)
            if pending.get("action") in md_execution.STARTS | {"cancel_md_stage"}:
                found = md_execution.reconcile(root, state, pending)
                changed(folder, state, "md_admission_reconciled")
                return response(state, found)
            if not pending.get("submission"):
                # These operations only read sources or write immutable local
                # evidence. The durable workflow still contains the preceding
                # committed assets; do not adopt an unfinished artifact by path.
                if not local_operation(pending):
                    raise ValueError("Uncertain execution lacks submission identity; keep blocked for reconciliation")
                state["pending"] = None
                changed(folder, state, "local_operation_reconciled", {"operation_id": pending["operation_id"]})
                return response(state, {"operation_state": "RESTORED_COMMITTED_ASSETS", "calculation_submitted": False,
                    "partial_artifacts_adopted": False, "automatic_retry": False})
            from covscreen.submission_identity import lookup_submission
            found = lookup_submission(root, pending["submission"])
            if found.get("registered_job_observed"):
                verify_preparation(root, state, found["run_id"])
                state["preparation"] = {"run_id": found["run_id"], "state": found["job_state"]}
                state.update(pending=None, stage="RECEPTOR_PREPARATION")
                changed(folder, state, "reconciled", {"run_id": found["run_id"]})
            return response(state, found)
        reason = args["reason"]
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000:
            raise ValueError("A concrete reason of 5-2000 characters is required")
        if action == "suspend":
            changed(folder, state, action, {"reason": reason})
            return response(state, {"jobs_cancelled": False, "session_binding": "suspended"})
        if action == "select_site":
            verified = identity(root, state)
            site = args["site"]
            if (not isinstance(site, str) or not re.fullmatch(r"C[1-9][0-9]{0,4}", site)
                or int(site[1:]) not in verified["identity"]["candidates"][0]["canonical_cysteines"]):
                raise ValueError("Select a cysteine in the verified canonical sequence")
            state.update(site_selection={"site": site, "reason": reason, "reactivity_validated": False}, stage="STRUCTURE_REVIEW")
        elif action == "reconsider":
            destination = args["return_to"]
            if destination not in {"target", "site", "structure"}:
                raise ValueError("Reconsider target, site or structure")
            if destination != "target":
                identity(root, state)
            if destination == "structure" and not state["site_selection"]:
                raise ValueError("No selected site to retain")
            invalid = ASSETS if destination == "target" else ASSETS[1:] if destination == "site" else ASSETS[2:]
            vs_assets = ('vs_prescreen', 'vs_input', 'vs_environment_review', 'vs_plan', 'vs_runtime',
                         'vs_execution', 'vs_input_error', 'lo_input', 'lo_plan', 'lo_runtime', 'lo_plan_history',
                         'lo_environment_review', 'lo_input_error', 'lo_execution') if state.get('vs_prescreen') else ()
            archive_branch(state, reason, retain=ASSETS + vs_assets, invalidate=invalid + vs_assets)
            if state.get('historical_md_source'):
                archive_branch(state, reason, retain=('historical_md_source',), invalidate=('historical_md_source',))
            state["stage"] = "STRUCTURE_REVIEW" if destination == "structure" else "TARGET_REVIEW"
        changed(folder, state, action, {"reason": reason})
        return response(state)


def receptor_start_request(root, args):
    folder = directory(root, args["workflow_id"])
    with version_lock(folder, ".workflow.lock"):
        state = load(folder)
        revision(state, args["expected_revision"])
        return receptor_request(root, state, args)


def bound_receptor_arguments(root, state, args):
    if (args.get("action") != "start" or state["stage"] != "RECEPTOR_PLAN_READY" or state["pending"]
        or set(args) - {"action", "workflow_id", "expected_revision", "human_approved", "approval_note"}):
        raise ValueError("Receptor execution requires the current frozen workflow plan")
    structure(root, state)
    return {**state["receptor_plan"]["arguments"], "action": "start",
            "human_approved": args.get("human_approved", False), "approval_note": args.get("approval_note")}


def receptor_request(root, state, args):
    parameters = bound_receptor_arguments(root, state, args)
    request = api._receptor_preparation_request(root, parameters, execution_mode="execute")
    return request.model_copy(update={"request_id": state["receptor_plan"]["request_id"]})


def verify_preparation(root, state, run_id):
    from covscreen.execution import JobManager
    from covscreen.native_approval import verify_spec, decode
    from covscreen.approval_scope import normalize_spec
    spec = JobManager(root).registry.load_spec(run_id)
    verify_spec(spec)
    expected = state["receptor_plan"]["approved_scope"]
    proof = decode(spec.provenance["native_approval"])
    if (decode(proof["approval"]).get("scope") != expected or normalize_spec(spec) != expected["spec"]
        or spec.request_id != state["receptor_plan"]["request_id"]):
        raise ValueError("Receptor job differs from the workflow's exact native approval")
    return spec


def step(root, tool, *, workflow_id, expected_revision, action, **args):
    if action in lo_input.ACTIONS:
        return lo_input.step(root, tool, workflow_id, expected_revision, action, args)
    if action in lo_execution.ACTIONS:
        return lo_execution.step(root, tool, workflow_id, expected_revision, action, args)
    if action in vs_prescreen.ACTIONS:
        return vs_prescreen.step(root, tool, workflow_id, expected_revision, action, args)
    if action in vs_input.ACTIONS:
        return vs_input.step(root, tool, workflow_id, expected_revision, action, args)
    if action in vs_execution.ACTIONS:
        return vs_execution.step(root, tool, workflow_id, expected_revision, action, args)
    if action in md_execution.ACTIONS:
        return md_execution.step(root, tool, workflow_id, expected_revision, action, args)
    from .screening_handoff import HOST_ACTIONS
    if action in HOST_ACTIONS:
        raise ValueError("Screening planning requires the authenticated host adapter; model arguments cannot grant authority")
    folder = directory(root, workflow_id)
    with version_lock(folder, ".workflow.lock"):
        state = load(folder)
        revision(state, expected_revision)
        visible = view(state)["tools"].get(tool)
        if not visible or action not in visible["actions"]:
            raise ValueError("Tool/action unavailable at the current workflow stage")
        extra = {"human_approved", "approval_note"} if tool == RECEPTOR and action == "start" else set()
        if set(args) - set(FIELDS[action]) - extra:
            raise ValueError("Workflow binds scientific identity; unexpected arguments")
        submission = None
        approved = None
        if tool == RECEPTOR and action == "start":
            from covscreen.native_approval import CURRENT, NativeApprovalDenied, decode
            if not CURRENT.get():
                raise NativeApprovalDenied("Workflow execution requires exact native approval")
            request = receptor_request(root, state, {"action": action, **args})
            submission = {"project_id": request.project_id, "request_id": request.request_id, "execution_mode": "execute"}
            approved = decode(CURRENT.get())["scope"]
        observing = action.startswith("read_") or action in {"status", "screening_profiles"}
        before = deepcopy(state)
        if not observing:
            if approved:
                state["receptor_plan"]["approved_scope"] = approved
            # Approval scope and exact lookup identity commit in the same atomic
            # write as pending; even an immediate crash remains reconcilable.
            begin_operation(folder, state, tool, action, submission=submission)
        try:
            result = execute_step(root, state, tool, action, args, before)
        except Exception as error:
            # Keep possibly dispatched work pending; read-only or local immutable
            # artifact failures can be corrected without adopting partial data.
            pending = state["pending"] if action == "start" else None
            approved = state["receptor_plan"].get("approved_scope") if state["receptor_plan"] else None
            state = before
            state["pending"] = pending
            if approved:
                state["receptor_plan"]["approved_scope"] = approved
            if not observing:
                changed(folder, state, "operation_uncertain" if pending else "tool_failed",
                        {"tool": tool, "action": action, "error": str(error)})
            raise
        state["pending"] = None
        # begin_operation persisted pending even when deterministic work reused
        # exactly the same asset. Always commit its completion; otherwise disk
        # retains a phantom pending operation while the response looks ready.
        if not observing or state != before:
            changed(folder, state, action, {"tool": tool})
        return response(state, result)


def execute_step(root, state, tool, action, args, before):
    if tool == RESEARCH:
        if action in historical_md.ACTIONS:
            return historical_md.execute(root, state, action, args)
        if action in {"prepare_screening_plan", "read_screening_plan"}:
            from . import screening_plan
            structure(root, state)
            verify_preparation(root, state, state["preparation"]["run_id"])
            if action == "read_screening_plan":
                return screening_plan.read(root, state)
            if set(args) != {"scientific_review", "max_points"}:
                raise ValueError("Scientific review and a proposed budget are required")
            result = screening_plan.freeze(root, state, args["scientific_review"], args["max_points"])
            updated = reference(result, "screening-plan-")
            if state.get("screening_plan") and state["screening_plan"] != updated:
                archive_branch(state, "Scientific review or proposed budget changed", retain=ASSETS, invalidate=("screening_plan",))
            state.update(screening_plan=updated, stage="SCREENING_PLAN_READY")
            return result
        if action in {"screening_profiles", "prepare_screening_inputs", "read_screening_inputs"}:
            from . import screening_inputs
            structure(root, state)
            verify_preparation(root, state, state["preparation"]["run_id"])
            if action == "screening_profiles":
                return screening_inputs.profiles(state)
            if action == "read_screening_inputs":
                return screening_inputs.read(root, state)
            if set(args) != {"profile"}:
                raise ValueError("Select an explicit registered screening profile")
            result = screening_inputs.freeze(root, state, args["profile"])
            updated = reference(result, "screening-inputs-")
            if state.get("screening_inputs") and state["screening_inputs"] != updated:
                archive_branch(state, "Screening input version was replaced", retain=ASSETS, invalidate=("screening_inputs", "screening_plan"))
                state["stage"] = "SCREENING_PLAN"
            state["screening_inputs"] = updated
            return result
        if action in {"review_receptor", "read_screening_receptor"}:
            from . import receptor_handoff
            structure(root, state)
            verify_preparation(root, state, state["preparation"]["run_id"])
            if action == "read_screening_receptor":
                return receptor_handoff.read(root, state)
            result = receptor_handoff.freeze(root, state)
            if state.get("screening_receptor") and state["screening_receptor"] != reference(result, "screening-receptor-"):
                archive_branch(state, "Receptor evidence was re-evaluated with the current audit implementation",
                               retain=ASSETS, invalidate=("screening_receptor", "screening_inputs", "screening_plan"))
                state["stage"] = "SCREENING_PLAN"
            state["screening_receptor"] = reference(result, "screening-receptor-")
            return result
        if action == "read_target":
            return identity(root, state)
        if action == "resolve_target":
            if args:
                if set(args) != {"target_artifact_id", "target_manifest_sha256"}:
                    raise ValueError("Target reuse requires both ID and hash")
                result = {"artifact_id": args["target_artifact_id"], "manifest_sha256": args["target_manifest_sha256"]}
            else:
                result = api.library_first_resolve_target_tool(root, state["target"]["target"], state["target"]["taxon"], action="search")
            state["target_identity"] = reference(result, "target-")
            identity(root, state)
            return result
        if action == "read_structures":
            return discovery(root, state)
        if action == "discover_structures":
            identity(root, state)
            if "discovery_artifact_id" in args or "discovery_manifest_sha256" in args:
                if set(args) != {"discovery_artifact_id", "discovery_manifest_sha256"}:
                    raise ValueError("Discovery reuse requires only its ID and hash")
                result = {"artifact_id": args["discovery_artifact_id"], "manifest_sha256": args["discovery_manifest_sha256"]}
            else:
                result = api.library_first_discover_structures_tool(root, state["target_identity"]["id"],
                    state["target_identity"]["sha256"], action="search", **args)
            state["discovery"] = reference(result, "structures-")
            discovery(root, state)
            state["stage"] = "STRUCTURE_SELECTION"
            return result
        if action == "read_structure":
            return structure(root, state)
        if action == "acquire_structure":
            discovery(root, state)
            if "receptor_input_artifact_id" in args or "manifest_sha256" in args:
                if set(args) != {"receptor_input_artifact_id", "manifest_sha256"}:
                    raise ValueError("Structure reuse requires only its ID and hash")
                result = {"artifact_id": args["receptor_input_artifact_id"], "manifest_sha256": args["manifest_sha256"]}
            else:
                result = api.library_first_acquire_structure_tool(root, state["discovery"]["id"],
                    state["discovery"]["sha256"], cys=int(state["site_selection"]["site"][1:]), action="acquire", **args)
            state["receptor_input"] = reference(result, "receptor-input-")
            structure(root, state)
            state["stage"] = "RECEPTOR_INPUT_READY"
            return result
    if tool == RECEPTOR:
        structure(root, state)
        if action == "plan":
            parameters = {"receptor_input_artifact_id": state["receptor_input"]["id"],
                          "manifest_sha256": state["receptor_input"]["sha256"], **args}
            result = api.library_first_prepare_receptor_tool(root, {"action": "plan", **parameters})
            if state["receptor_plan"]:
                archive_branch(state, "Receptor planning superseded the prior frozen plan", retain=ASSETS, invalidate=("receptor_plan",))
            state["stage"] = "RECEPTOR_INPUT_READY"
            if result["status"] == "PLANNED":
                request_id = state["workflow_id"] + "-receptor-r" + str(state["revision"] + 1)
                request = api._receptor_preparation_request(root, parameters, execution_mode="plan").model_copy(update={"request_id": request_id})
                from covscreen.execution import JobManager
                result["spec"] = JobManager(root).build_spec(request).model_dump(mode="json")
                state.update(receptor_plan={"arguments": parameters, "request_id": request_id, "spec": result["spec"]}, stage="RECEPTOR_PLAN_READY")
            return result
        if action == "start":
            from covscreen.execution import JobManager
            from covscreen.native_approval import CURRENT, NativeApprovalDenied
            if not CURRENT.get():
                raise NativeApprovalDenied("Workflow execution requires exact native approval")
            request = receptor_request(root, before, {"action": action, **args})
            job = JobManager(root).submit(request)
            verify_preparation(root, state, job.run_id)
            result = {"job": job.model_dump(mode="json")}
            state.update(preparation={"run_id": job.run_id, "state": job.state}, stage="RECEPTOR_PREPARATION")
            return result
        if action == "status":
            prep = state["preparation"]
            verify_preparation(root, state, prep["run_id"])
            result = api.library_first_prepare_receptor_tool(root, {"action": "status", "run_id": prep["run_id"]})
            prep["state"] = result["job"]["state"]
            if prep["state"] == "COMPLETED":
                from covscreen.execution import JobManager
                manifest = JobManager(root).registry.job_dir(prep["run_id"]) / "artifacts/receptor-preparation/prepared/prepared-receptor.json"
                prep["manifest_sha256"] = sha256_file(manifest)
                if not state["receptor_context"]:
                    state["stage"] = "RECEPTOR_REVIEW"
            elif prep["state"] in {"FAILED", "TIMED_OUT", "CANCELLED"}:
                state["stage"] = "RECEPTOR_FAILED"
            return result
        if action == "assess_context":
            prep = state["preparation"]
            verify_preparation(root, state, prep["run_id"])
            result = api.library_first_assess_receptor_context_tool(root, {
                "receptor_input_artifact_id": state["receptor_input"]["id"], "manifest_sha256": state["receptor_input"]["sha256"],
                "preparation_run_id": prep["run_id"], "prepared_receptor_manifest_sha256": prep["manifest_sha256"]})
            updated = reference(result, "receptor-context-")
            if state["receptor_context"] != updated:
                archive_branch(state, "Receptor context changed", retain=ASSETS,
                               invalidate=("screening_receptor", "screening_inputs", "screening_plan"))
            state.update(receptor_context=updated, stage="SCREENING_PLAN_READY" if state.get("screening_plan") else "SCREENING_PLAN")
            return result
    raise ValueError("Unknown workflow stage action")
