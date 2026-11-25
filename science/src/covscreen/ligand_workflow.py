"""Session-selectable Ligand-first navigation over the existing science tools.

Each workflow is independent, revision checked and durable. Navigation never
approves computation; evidence revisions invalidate the active design handoff.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
import json
from uuid import uuid4

from . import target_hypothesis as evidence_tools
from . import ligand_design as design_tools
from .ligand_parent_evidence import latest_version, version_lock
from .workflow_store import load, save
from .workflow_protocol import archive_branch, begin_operation, changed, envelope, revision
from .ligand_md_source import record as md_source_record
from . import ligand_md_preparation as md_preparation

ENTRY = "covalent_ligand_workflow"
EVIDENCE = "covalent_ligand_evidence"
DESIGN = "covalent_ligand_design_step"
INTERFACE = {
    "schema": "research-workflow-interface-v1", "route": "ligand-first",
    "protocol": "ligand-workflow-v1", "id_pattern": "^ligand-flow-[a-f0-9]{24}$",
    "entry": ENTRY, "tools": [ENTRY, EVIDENCE, DESIGN],
    "start_fields": ["action", "request_id", "target", "site", "taxon", "evidence_id", "workflow_id"],
    "legacy_names": ["covalent_ligand_design", "covalent_plan_ligand_first", "covalent_evaluate_ligand_first_candidate"],
    "legacy_prefixes": ["covalent_ligand_first_"],
    "refreshable": {EVIDENCE: ["read", "read_sources", "read_parent_evidence", "supplement", "search_parent_literature", "measure_parent"]},
}
READS = {"read", "read_parent_evidence", "read_sources", "search_parent_index", "export_parent_report", "export_design_report", "export_covdock_report"}
ADDITIONS = {"supplement", "search_parent_literature", "measure_parent"}
LOCAL_OPERATIONS = {
    EVIDENCE: {"resolve_target", "supplement", "search_parent_literature", "measure_parent", "finalize"},
    DESIGN: {"prepare", "generate", "plan_pose", "check_pose_runtime", "deliver"},
}
FIELDS = {
    "search_parent_index": ["offset", "limit"],
    "resolve_target": ["target_artifact_id", "target_manifest_sha256"], "read_target": [],
    "collect": [], "read": [], "read_parent_evidence": [], "read_sources": ["source_ids"],
    "supplement": ["source_ids"], "search_parent_literature": ["query", "molecule_id"],
    "measure_parent": ["molecule_id", "pdb_id", "component", "label_chain", "ligand_chain",
                       "ligand_residue", "model", "altloc"],
    "finalize": ["hypothesis"],
    "export_parent_report": [],
    "export_design_report": ["design_id"],
    "export_covdock_report": [],
}


def directory(root: Path, workflow_id: str) -> Path:
    if not re.fullmatch(r"ligand-flow-[a-f0-9]{24}", workflow_id):
        raise ValueError("Invalid Ligand-first workflow ID")
    return root / "state/ligand-workflows" / workflow_id


def archive(state: dict, reason: str):
    archive_branch(state, reason, retain=("parent_pair_review", "docking_decision", "conformer_selection", "construct_review", "vs_receptor", "vs_input_history", "vs_execution", "vs_input", "vs_plan", "vs_runtime", "vs_environment_review", "vs_plan_history", "vs_input_error", "vs_review_error", "evidence_id", "proposal", "design_id", "pose_execution", "refinement_input", "pose_report", "af3_execution", "covdock_execution", "md_graph_execution", "md_parameterization", "md_parameterization_plan_history", "md_system_preparation", "md_short_qc", "md_equilibration", "md_preparation_plan_history", "md_preparation_attempt_history", "historical_md_source"),
                   invalidate=("parent_pair_review", "docking_decision", "conformer_selection", "construct_review", "vs_receptor", "vs_input_history", "vs_execution", "vs_input", "vs_plan", "vs_runtime", "vs_environment_review", "vs_plan_history", "vs_input_error", "vs_review_error", "proposal", "design_id", "pose_execution", "refinement_input", "pose_report", "af3_execution", "covdock_execution", "md_graph_execution", "md_parameterization", "md_parameterization_plan_history", "md_system_preparation", "md_short_qc", "md_equilibration", "md_preparation_plan_history", "md_preparation_attempt_history", "historical_md_source"))


def refresh(root: Path, folder: Path, state: dict):
    """External additive evidence invalidates prior handoffs, never silently rebases them."""
    if state.get('historical_md_source') and not state.get('pending'):
        from .ligand_historical_md_source import refresh as refresh_historical
        before = json.dumps(state['historical_md_source'], sort_keys=True)
        refresh_historical(root, state)
        if state['historical_md_source'].get('md_selection'):
            from .ligand_md_selection import refresh as refresh_selection
            refresh_selection(root, state)
        if before != json.dumps(state['historical_md_source'], sort_keys=True):
            changed(folder, state, 'historical_md_source_refreshed')
    if md_preparation.active(state) and not state.get('pending'):
        before = json.dumps(state, sort_keys=True)
        diagnosis = md_preparation.revalidate_saved(root, state)
        if before != json.dumps(state, sort_keys=True):
            changed(folder, state, 'md_preparation_evidence_refreshed', {'error': diagnosis})
        return
    if state.get('md_parameterization') and not state.get('pending'):
        from .ligand_md_parameterization import revalidate_saved
        before = json.dumps(state, sort_keys=True)
        diagnosis = revalidate_saved(root, state)
        if before != json.dumps(state, sort_keys=True):
            changed(folder, state, 'md_parameterization_evidence_refreshed', {'error': diagnosis})
        return
    if (state.get('md_graph_execution') or {}).get('run_id') and not state.get('pending'):
        from .ligand_md_graph_execution import revalidate_saved
        before = json.dumps([state['stage'], state['md_graph_execution'], md_source_record(state)], sort_keys=True)
        diagnosis = revalidate_saved(root, state)
        if before != json.dumps([state['stage'], state['md_graph_execution'], md_source_record(state)], sort_keys=True):
            changed(folder, state, 'md_graph_evidence_refreshed', {'error': diagnosis})
        return
    if (state.get('vs_execution') or {}).get('run_id') and not state.get('pending'):
        from .ligand_vs_execution import revalidate_saved
        before = json.dumps([state['stage'], state['vs_execution']], sort_keys=True)
        diagnosis = revalidate_saved(root, state)
        if before != json.dumps([state['stage'], state['vs_execution']], sort_keys=True):
            changed(folder, state, 'vs_execution_evidence_refreshed', {'error': diagnosis})
        if state['stage'] in {'VS_EXECUTION_RUNNING', 'VS_EXECUTION_REVIEW'}:
            return
    from .ligand_direct_lo import revalidate_saved as revalidate_direct_lo
    before = json.dumps(state.get('refinement_input'), sort_keys=True)
    diagnosis = revalidate_direct_lo(root, state)
    if before != json.dumps(state.get('refinement_input'), sort_keys=True):
        changed(folder, state, 'direct_lo_source_refreshed', {'error': diagnosis})
    from .ligand_vs_refinement import revalidate_saved as revalidate_vs_refinement
    before = json.dumps(state.get('refinement_input'), sort_keys=True)
    diagnosis = revalidate_vs_refinement(root, state)
    if before != json.dumps(state.get('refinement_input'), sort_keys=True):
        changed(folder, state, 'vs_refinement_source_refreshed', {'error': diagnosis})
    if (state.get('covdock_execution') or {}).get('run_id') and not state.get('pending'):
        from .ligand_covdock_execution import revalidate_saved
        before = json.dumps([state['stage'], state['covdock_execution']], sort_keys=True)
        diagnosis = revalidate_saved(root, state)
        if before != json.dumps([state['stage'], state['covdock_execution']], sort_keys=True):
            changed(folder, state, 'covdock_evidence_refreshed', {'error': diagnosis})
        return
    if not state.get("evidence_id") or state.get("pending") or state["stage"] in {"VS_EXECUTION_RUNNING", "POSE_EXECUTION_RUNNING", "AF3_EXECUTION_RUNNING", "RECEPTOR_PREPARATION_RUNNING", "COVDOCK_EXECUTION_RUNNING", "MD_GRAPH_RUNNING", "MD_PARAMETERIZATION_RUNNING", "MD_SYSTEM_PREPARATION_RUNNING", "MD_SHORT_QC_RUNNING", "MD_EQUILIBRATION_RUNNING"}:
        return
    latest = latest_version(root, state["evidence_id"])
    if latest != state["evidence_id"]:
        current = evidence_tools.read(root, latest)
        if current["state"] != "EVIDENCE_READY":
            raise ValueError("Latest evidence is not committed")
        require_scope(state, current["evidence"])
        old = state["evidence_id"]
        archive(state, "Evidence changed; comparison and design must be revalidated")
        state.update(evidence_id=latest, stage="PARENT_REVIEW")
        changed(folder, state, "evidence_invalidated", {"previous_evidence_id": old, "evidence_id": latest})
    from .ligand_vs_input import revalidate_saved as revalidate_vs
    before = json.dumps(state, sort_keys=True)
    diagnosis = revalidate_vs(root, state)
    if before != json.dumps(state, sort_keys=True):
        changed(folder, state, 'vs_evidence_refreshed', {'error': diagnosis})
    from .ligand_refinement_target import revalidate_saved
    diagnosis = revalidate_saved(root, state)
    if diagnosis:
        if state['stage'] in {'AF3_PLAN_READY', 'AF3_RUNTIME_REQUIRED'}:
            state['stage'] = ('VS_REFINEMENT_READY' if (state.get('refinement_input') or {}).get('source_kind')
                              == 'registered-ligand-vs-review-v1' else 'POSE_EXECUTION_REVIEW')
        changed(folder, state, "refinement_target_rejected", diagnosis)
    from .ligand_af3_execution import revalidate_saved as revalidate_af3
    before = json.dumps(state.get('af3_execution'), sort_keys=True)
    diagnosis = revalidate_af3(root, state)
    if before != json.dumps(state.get('af3_execution'), sort_keys=True):
        changed(folder, state, 'af3_evidence_refreshed', {'error': diagnosis})
    from .ligand_receptor_preparation import revalidate_saved as revalidate_receptor
    diagnosis = revalidate_receptor(root, state)
    if diagnosis:
        changed(folder, state, 'receptor_input_rejected', {'reason': diagnosis})
    from .ligand_receptor_source import context as receptor_context
    from .ligand_receptor_execution import revalidate_saved as revalidate_prepared
    before = json.dumps(receptor_context(state).get('receptor_preparation'), sort_keys=True)
    diagnosis = revalidate_prepared(root, state)
    if before != json.dumps(receptor_context(state).get('receptor_preparation'), sort_keys=True):
        changed(folder, state, 'receptor_evidence_refreshed', {'error': diagnosis})
    from .ligand_covdock_input import revalidate_saved as revalidate_covdock
    diagnosis = revalidate_covdock(root, state)
    if diagnosis:
        changed(folder, state, 'covdock_input_rejected', {'reason': diagnosis})


def require_scope(state: dict, evidence: dict):
    if any(evidence.get(key) != value for key, value in state["target"].items()):
        raise ValueError("Evidence belongs to another target/site/species")


def view(state: dict) -> dict:
    stage = state["stage"]
    navigation = ["status", "reconsider", "suspend"] if not state.get("pending") else ["status"]
    pending = state.get("pending") or {}
    if (pending.get("action") in {"start_vs", "start_pose", "start_af3", "start_receptor_preparation", "start_covdock", "start_md_graph", "start_md_parameterization", "start_md_system_preparation", "start_md_short_qc", "start_md_equilibration"}
        or pending.get("action") in LOCAL_OPERATIONS.get(pending.get("tool"), set())
        or (pending.get("tool") == EVIDENCE and pending.get("action") == "collect" and pending.get("recovery"))):
        navigation.append("reconcile")
    if stage in {"VS_EXECUTION_RUNNING", "POSE_EXECUTION_RUNNING", "AF3_EXECUTION_RUNNING", "RECEPTOR_PREPARATION_RUNNING", "COVDOCK_EXECUTION_RUNNING", "MD_GRAPH_RUNNING", "MD_PARAMETERIZATION_RUNNING", "MD_SYSTEM_PREPARATION_RUNNING", "MD_SHORT_QC_RUNNING", "MD_EQUILIBRATION_RUNNING"}:
        navigation = ["status", "suspend"]
    graph = state.get('md_graph_execution') or {}
    if graph and not pending and (not graph.get('job_identity_verified') or graph.get('job_state') not in {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}):
        navigation = ['status', 'suspend']
    parameterization = state.get('md_parameterization') or {}
    if parameterization.get('approved_scope') and not pending and (not parameterization.get('job_identity_verified')
            or parameterization.get('job_state') not in {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}):
        navigation = ['status', 'suspend']
    for name in md_preparation.ENGINES:
        record = state.get('md_' + name) or {}
        if record.get('approved_scope') and not pending and (not record.get('job_identity_verified')
                or record.get('job_state') not in md_preparation.TERMINAL):
            navigation = ['status', 'suspend']
    if state["branches"]:
        navigation.append("inspect_branch")
    if stage == "BRANCH_REPORTED" and state.get("design_id") and not state.get("pending"):
        navigation.append("reopen_design")
    fields = ["action", "workflow_id", "expected_revision"]
    if not pending:
        fields.append("reason")
    if "inspect_branch" in navigation:
        fields.append("branch_index")
    if stage == "TARGET_REVIEW" and not state.get("pending"):
        navigation.append("revise_target")
        fields += ["target", "taxon"]
        if state.get("target_identity"):
            navigation.append("select_site")
            fields.append("site")
    tools = {ENTRY: {"actions": navigation, "fields": fields}}
    if stage == 'INTAKE' and not pending and not state.get('evidence_id'):
        navigation.append('attach_md_source')
        fields += ['source_artifact_id', 'source_manifest_sha256']
    actions = []
    if not state.get("pending") and not state.get('historical_md_source'):
        if stage == "TARGET_REVIEW":
            actions = ["read_target", "search_parent_index"] if state.get("target_identity") else ["resolve_target"]
        elif not state.get("evidence_id"):
            actions = ["collect"]
        elif stage == "EVIDENCE_PENDING":
            actions = ["read"]
        elif stage != "EVIDENCE_FAILED":
            actions = sorted(READS | ADDITIONS | ({"finalize"} if stage == "PARENT_REVIEW" else set()))
    if not state.get("proposal"):
        actions = [action for action in actions if action != "export_parent_report"]
    if not state.get("design_id") and not any(b.get("design_id") for b in state.get("branches", [])):
        actions = [action for action in actions if action != "export_design_report"]
    if actions:
        if stage in {"VS_EXECUTION_RUNNING", "POSE_EXECUTION_RUNNING", "AF3_EXECUTION_RUNNING", "RECEPTOR_PREPARATION_RUNNING", "COVDOCK_EXECUTION_RUNNING", "MD_GRAPH_RUNNING", "MD_PARAMETERIZATION_RUNNING", "MD_SYSTEM_PREPARATION_RUNNING", "MD_SHORT_QC_RUNNING", "MD_EQUILIBRATION_RUNNING"}:
            actions = sorted(READS - ({"export_parent_report"} if not state.get("proposal") else set()))
        if not ((state.get('covdock_execution') or {}).get('report')
                and (state.get('covdock_execution') or {}).get('evaluation_verification') == 'verified'
                and not (state.get('covdock_execution') or {}).get('evidence_error')):
            actions = [action for action in actions if action != 'export_covdock_report']
        tools[EVIDENCE] = {"actions": actions, "fields": sorted({"action", "workflow_id", "expected_revision"}
            | {field for action in actions for field in FIELDS[action]})}
    design_actions = {
        "DESIGN_READY": ["prepare"], "DESIGN_INPUTS_READY": ["generate"],
        "NEEDS_POSE_EVALUATION": ["plan_pose", "deliver"],
        "NO_GRAPH_CANDIDATES": ["deliver"], "DESIGN_REVIEW_FAILED": ["deliver"],
        "POSE_PLAN_READY": ["check_pose_runtime", "deliver"], "POSE_INPUTS_BLOCKED": ["deliver"],
    }.get(stage, []) if not state.get("pending") else []
    if not state.get("pending"):
        if stage == 'HISTORICAL_MD_SOURCE_REVIEW':
            source = state['historical_md_source']
            if source.get('verification') == 'source_chain_verified':
                design_actions = ['prepare_historical_md_candidate']
                if source.get('md_selection_verification') == 'verified':
                    design_actions = ['check_md_graph_runtime']
                    if (source.get('md_graph_plan') or {}).get('state') == 'plan_compiled' and not source.get('md_graph_plan_error'):
                        design_actions.append('start_md_graph')
            if source.get('md_selection'):
                design_actions.append('release_md_candidate')
        if stage == "POSE_PLAN_READY" and state.get("pose_execution"):
            design_actions.append("start_pose")
        if stage in {"POSE_EXECUTION_RUNNING", "POSE_EXECUTION_REVIEW"}:
            design_actions = ["pose_status"]
            record = state.get("pose_execution") or {}
            if record.get("job_state") == "COMPLETED" and record.get("return_code") == 0:
                design_actions += ["evaluate_pose", "report_pose"]
                if (record.get("evaluation_verification") == "verified"
                        and record["evaluation"]["passing_candidate_count"] > 0):
                    design_actions.append("prepare_refinement")
                    if state.get("refinement_input"):
                        design_actions.append("prepare_refinement_target")
                        if state['refinement_input'].get('target'):
                            design_actions.append('check_af3_runtime')
        if stage in {'AF3_RUNTIME_REQUIRED', 'AF3_PLAN_READY'}:
            design_actions = ['check_af3_runtime']
            if stage == 'AF3_PLAN_READY' and (state.get('refinement_input') or {}).get('target'):
                design_actions.append('start_af3')
        if stage == 'DIRECT_LO_READY':
            design_actions = ['prepare_direct_lo', 'prepare_direct_lo_target']
        if stage == 'VS_REFINEMENT_READY':
            design_actions = ['prepare_vs_refinement', 'prepare_vs_refinement_target']
            if (state.get('refinement_input') or {}).get('target'):
                design_actions.append('check_af3_runtime')
        if stage in {'AF3_EXECUTION_RUNNING', 'AF3_EXECUTION_REVIEW'}:
            design_actions = ['af3_status']
            if stage == 'AF3_EXECUTION_REVIEW':
                design_actions += ['evaluate_af3', 'report_af3']
        from .ligand_receptor_source import context as receptor_context, planning_available, pre_vs, policy_replanning_available
        if planning_available(state):
            design_actions.append('prepare_receptor_input')
            if receptor_context(state).get('receptor_input'):
                design_actions.append('check_receptor_runtime')
                preparation = receptor_context(state).get('receptor_preparation') or {}
                if preparation.get('runtime_check') == 'CPU_PREPARATION_PLAN_COMPILED' and not preparation.get('run_id'):
                    design_actions.append('start_receptor_preparation')
        if stage in {'RECEPTOR_PREPARATION_RUNNING', 'RECEPTOR_PREPARATION_REVIEW'}:
            design_actions = ['receptor_preparation_status', 'refresh_receptor_preparation']
            record = receptor_context(state).get('receptor_preparation') or {}
            if (policy_replanning_available(state) and record.get('job_state') == 'FAILED'
                    and not receptor_context(state).get('receptor_relaxation_selection')):
                design_actions += ['select_restrained_receptor_policy', 'select_local_receptor_policy']
            if (policy_replanning_available(state) and record.get('job_state') == 'FAILED'
                    and (receptor_context(state).get('receptor_relaxation_selection') or {}).get('version') == 'v2-local'):
                design_actions.append('select_bounded_local_receptor_policy')
            if (policy_replanning_available(state) and record.get('job_state') == 'FAILED'
                    and (receptor_context(state).get('receptor_relaxation_selection') or {}).get('version') == 'v2-local-bounded'):
                design_actions.append('select_block_local_receptor_policy')
            if record.get('job_state') == 'COMPLETED' and record.get('return_code') == 0:
                design_actions += ['evaluate_receptor', 'report_receptor']
                if (record.get('spec') or {}).get('parameters', {}).get('relaxation_policy_version') in {'v2', 'v2-local', 'v2-local-bounded', 'v2-local-block'}:
                    design_actions += ['read_receptor_motion', 'review_receptor_motion']
                if (record.get('evaluation_verification') == 'verified' and record.get('receptor_prepared') is True
                        and not record.get('evidence_error')):
                    if pre_vs(state):
                        design_actions.append('use_prepared_receptor_for_vs')
                    else:
                        design_actions.append('prepare_covdock_input')
                    if (state.get('refinement_input') or {}).get('covdock_input'):
                        design_actions += ['read_covdock_environment', 'review_covdock_environment', 'check_covdock_runtime']
                    plan = (state.get('refinement_input') or {}).get('covdock_plan') or {}
                    if plan.get('runtime_check') == 'REGISTERED_COVDOCK_PLAN_COMPILED' and not state.get('covdock_execution'):
                        design_actions.append('start_covdock')
        from .ligand_vs_receptor import refresh_available
        if refresh_available(state):
            design_actions.append('use_prepared_receptor_for_vs')
        if stage in {'COVDOCK_EXECUTION_RUNNING', 'COVDOCK_EXECUTION_REVIEW'}:
            from .covdock_gate import REVIEWABLE_BATCH_OUTCOMES
            design_actions = ['covdock_status']
            record = state.get('covdock_execution') or {}
            if (not record.get('evidence_error')
                    and (record.get('job_state'), record.get('return_code')) in REVIEWABLE_BATCH_OUTCOMES):
                design_actions += ['evaluate_covdock', 'report_covdock']
                if record.get('evaluation_verification') == 'verified':
                    design_actions.append('prepare_md_handoff')
                    if record.get('md_handoff'):
                        design_actions.append('select_md_candidate')
                        if record.get('md_selection_verification') == 'verified':
                            design_actions.append('check_md_graph_runtime')
                            plan = record.get('md_graph_plan') or {}
                            if (plan.get('state') == 'plan_compiled' and not record.get('md_graph_plan_error')
                                    and not state.get('md_graph_execution')):
                                design_actions.append('start_md_graph')
            if record.get('md_selection') and stage == 'COVDOCK_EXECUTION_REVIEW':
                design_actions.append('release_md_candidate')
        if stage in {'MD_GRAPH_RUNNING', 'MD_GRAPH_REVIEW'}:
            design_actions = ['md_graph_status']
            if (stage == 'MD_GRAPH_REVIEW' and not state.get('md_parameterization')
                    and (state.get('md_graph_execution') or {}).get('verification') == 'verified'):
                design_actions.append('plan_md_parameterization')
        from .ligand_md_parameterization import STAGES as PARAMETERIZATION_STAGES
        if stage in PARAMETERIZATION_STAGES:
            record = state.get('md_parameterization') or {}
            design_actions = ['md_parameterization_status'] if record.get('run_id') else []
            if not record.get('run_id') and not record.get('approved_scope'):
                design_actions = ['plan_md_parameterization', 'release_md_parameterization_plan']
                if stage == 'MD_PARAMETERIZATION_PLAN_READY' and not record.get('evidence_error'):
                    design_actions.append('start_md_parameterization')
        if (stage == 'MD_PARAMETERIZATION_REVIEW' and not state.get('md_system_preparation')
                and (state.get('md_parameterization') or {}).get('verification') == 'verified'):
            design_actions.append('plan_md_system_preparation')
        for name in md_preparation.ENGINES:
            if stage in md_preparation.stages(name):
                record = state.get('md_' + name) or {}
                design_actions = ['md_' + name + '_status'] if record.get('run_id') else []
                if not record.get('run_id') and not record.get('approved_scope'):
                    design_actions = ['plan_md_' + name, 'release_md_' + name + '_plan']
                    if stage == f'MD_{name.upper()}_PLAN_READY' and not record.get('evidence_error'):
                        design_actions.append('start_md_' + name)
                following = {'system_preparation': 'short_qc', 'short_qc': 'equilibration'}.get(name)
                if following and record.get('verification') == 'verified' and not state.get('md_' + following):
                    design_actions.append('plan_md_' + following)
                if record.get('job_identity_verified') and record.get('job_state') in {'FAILED', 'CANCELLED', 'TIMED_OUT'}:
                    design_actions.append('retry_md_' + name + '_plan')
    from .docking_policy import POLICY
    if "carsidock-cov" in POLICY["disabled_engines"]:
        design_actions = [action for action in design_actions if action not in {"plan_pose", "check_pose_runtime", "start_pose"}]
    from .ligand_direct_lo import available as direct_lo_available
    if direct_lo_available(state):
        design_actions[0:0] = ['prepare_docking_input', 'prepare_direct_lo']
    from .ligand_vs_input import available as vs_available, construct_available, construct_frozen
    if construct_available(state):
        design_actions.append('read_construct_evidence')
        if not construct_frozen(state):
            design_actions += ['review_construct', 'select_receptor_conformers']
    if vs_available(state):
        design_actions += (['read_vs_input', 'review_vs_environment', 'check_vs_runtime'] if state.get('vs_input')
                           else ['prepare_vs_input'])
        vs_runtime = state.get('vs_runtime') or {}
        if (POLICY['native_vs_workflow_ready'] and vs_runtime.get('state') == 'VS_PLAN_COMPILED'
                and not vs_runtime.get('execution_blocker')):
            design_actions.append('start_vs')
    if stage in {'VS_EXECUTION_RUNNING', 'VS_EXECUTION_REVIEW'} and not state.get('pending'):
        design_actions = ['vs_status']
        record = state.get('vs_execution') or {}
        if record.get('job_state') == 'COMPLETED' and record.get('return_code') == 0:
            design_actions += ['evaluate_vs','report_vs']
            if not record.get('evidence_error') and (record.get('evaluation_summary') or {}).get('passing_candidate_count', 0) > 0:
                design_actions.append('prepare_vs_refinement')
    if design_actions:
        tools[DESIGN] = {"actions": design_actions, "fields": ["action", "workflow_id", "expected_revision"]}
        if 'prepare_docking_input' in design_actions:
            tools[DESIGN]['fields'] += ['docking_route', 'reason']
        if 'prepare_vs_refinement' in design_actions:
            tools[DESIGN]['fields'] += ['candidate_ids', 'reason']
        if {'select_restrained_receptor_policy', 'select_local_receptor_policy', 'select_bounded_local_receptor_policy', 'select_block_local_receptor_policy'}.intersection(design_actions):
            tools[DESIGN]['fields'].append('reason')
        if 'review_receptor_motion' in design_actions:
            tools[DESIGN]['fields'].append('motion_review')
        if 'prepare_historical_md_candidate' in design_actions:
            tools[DESIGN]['fields'] += ['target_artifact_id', 'target_manifest_sha256', 'reason']
        if {"start_vs", "start_pose", "start_af3", "start_receptor_preparation", "start_covdock", "start_md_graph", "start_md_parameterization", "start_md_system_preparation", "start_md_short_qc", "start_md_equilibration"}.intersection(design_actions):
            tools[DESIGN]["fields"] += ["human_approved", "approval_note"]
        if {'review_covdock_environment', 'review_vs_environment'}.intersection(design_actions):
            tools[DESIGN]['fields'].append('environment_review')
        if 'select_receptor_conformers' in design_actions:
            tools[DESIGN]['fields'].append('conformer_selection')
        if 'review_construct' in design_actions:
            tools[DESIGN]['fields'].append('construct_review')
        if {'read_vs_input', 'read_covdock_environment', 'read_receptor_motion'}.intersection(design_actions):
            from .receptor_environment_review import EVIDENCE_READ_SCHEMA
            tools[DESIGN]['fields'] += list(EVIDENCE_READ_SCHEMA)
        if 'select_md_candidate' in design_actions:
            tools[DESIGN]['fields'] += ['compound_id', 'input_sdf_index', 'reason']
        elif 'release_md_candidate' in design_actions:
            tools[DESIGN]['fields'].append('reason')
    return {"protocol": "ligand-workflow-v1", "workflow_id": state["workflow_id"],
        "revision": state["revision"], "stage": stage, "tools": tools}


def response(state: dict, result: dict | None = None, *, compact=False) -> dict:
    from .docking_policy import POLICY
    from .ligand_receptor_source import context as receptor_context
    from .metric_contracts import workflow_metric_context, md_not_evaluated
    from .ligand_md_selection import selection_status
    from .ligand_md_graph_execution import projection
    from .ligand_md_parameterization import projection as parameterization_projection
    # Old tool hints refer to unscoped maintenance APIs; never return them as
    # instructions to the workflow's scientific agent.
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in {"next_call", "design_handoff"}}
        return [clean(v) for v in value] if isinstance(value, list) else value
    output = envelope(route="ligand-first", **outcome(state), **{
        "workflow": view(state), "target": state["target"], "evidence_id": state.get("evidence_id"),
        "metric_context": workflow_metric_context("ligand-first", state["stage"]),
        "md_admission": md_not_evaluated(),
        "docking_engine_policy": POLICY,
        "docking_decision": state.get('docking_decision'),
        "md_candidate_selection": selection_status(state),
        "target_identity": state.get("target_identity"), "site_selection": state.get("site_selection"),
        "design_id": state.get("design_id"), "proposal": state.get("proposal"),
        "parent_pair_review": state.get("parent_pair_review"),
        "pose_execution": state.get("pose_execution"),
        "vs_execution": state.get("vs_execution"),
        "vs_input": state.get("vs_input"),
        "vs_receptor": state.get("vs_receptor"),
        "vs_runtime": state.get("vs_runtime"),
        "vs_environment_review": state.get("vs_environment_review"),
        "construct_review": state.get('construct_review'),
        "conformer_selection": state.get('conformer_selection'),
        "vs_input_error": state.get("vs_input_error"),
        "vs_review_error": state.get("vs_review_error"),
        "af3_execution": state.get("af3_execution"),
        "covdock_execution": state.get("covdock_execution"),
        "historical_md_source": state.get('historical_md_source'),
        "md_graph_execution": projection(state),
        "md_parameterization": parameterization_projection(state),
        "md_system_preparation": md_preparation.projection(state, "system_preparation"),
        "md_short_qc": md_preparation.projection(state, "short_qc"),
        "md_equilibration": md_preparation.projection(state, "equilibration"),
        "md_preparation_attempt_history": [
            {key: item.get(key) for key in ('stage', 'run_id', 'request_id', 'job_state',
                'return_code', 'failure_reason', 'message', 'job_identity_verified')}
            for item in (state.get('md_preparation_attempt_history') or [])],
        "refinement_input": state.get("refinement_input"),
        "pose_report": state.get("pose_report"),
        "branches": state["branches"], "pending": state.get("pending"),
        "compute_submitted": bool((receptor_context(state).get('receptor_preparation') or {}).get('run_id'))
            if state['stage'].startswith('RECEPTOR_PREPARATION_') else
            bool((state.get('vs_execution') or {}).get('run_id')) if state.get('vs_input') else any((state.get(key) or {}).get('run_id') for key in
                                 ('vs_execution', 'pose_execution', 'af3_execution', 'covdock_execution', 'md_graph_execution', 'md_parameterization', 'md_system_preparation', 'md_short_qc', 'md_equilibration')),
        "result": clean(result) if result is not None else None,
        "research_responsibility": "For an open-ended design request, independently propose a bounded exploratory site hypothesis with a reason and then verify it using retrieved evidence. Do not ask the user to choose a research site or parent merely because several candidates exist. Sequence identity is not reactivity evidence; label prior knowledge and assumptions as unverified. Ask for clarification only when user intent or target identity cannot be resolved, or an actual user-owned constraint/authorization is missing. Honor explicit user requests to compare or choose sites.",
        "guidance": "Use reconsider with a reason to reopen parent selection. Supplement evidence at any reviewed stage; this invalidates the active handoff while retaining past branches. A blocked or delivered branch is not proof of target infeasibility. Runtime execution remains separately registered and approved."})
    direct = (state.get('refinement_input') or {}).get('source_kind') == 'sealed-ligand-direct-lo-v1'
    if state.get('vs_receptor') or direct:
        from .ligand_receptor_source import public_context, public_record, public_evaluation, pre_vs
        if state.get('vs_receptor'):
            output['vs_receptor'] = public_context(output['vs_receptor'])
        if direct:
            output['refinement_input'] = public_context(output['refinement_input'])
            if output['refinement_input'].get('covdock_plan'):
                output['refinement_input']['covdock_plan'] = public_record(output['refinement_input']['covdock_plan'])
            if output.get('result') and 'review_requirements' in output['result']:
                from .ligand_receptor_source import public_covdock_environment
                output['result'] = public_covdock_environment(output['result'], state['refinement_input'])
        if (pre_vs(state) or direct) and output.get('result'):
            for key, summarize in (('receptor_preparation', public_record), ('evaluation', public_evaluation),
                                   ('covdock_plan', public_record)):
                if output['result'].get(key):
                    output['result'][key] = summarize(output['result'][key])
    if compact or direct:
        # Planning must not bury current evidence beneath historical raw poses.
        # Keep the full persisted records and normal status/report behavior.
        for name in ('pose_execution', 'af3_execution', 'covdock_execution', 'vs_execution'):
            record = output.get(name)
            if record:
                output[name] = {key: record[key] for key in ('run_id', 'request_id', 'job_state',
                    'return_code', 'failure_reason', 'output_validation', 'evaluation_verification', 'evidence_error',
                    'report', 'evaluation_artifact') if key in record}
                evaluation = record.get('evaluation') or {}
                output[name]['assessment_summary'] = {key: evaluation[key] for key in
                    ('status', 'candidate_count', 'passing_candidate_count', 'md_ready') if key in evaluation}
        output['branches'] = [{key: branch[key] for key in ('branch_id', 'reason', 'stage', 'design_id')
                               if key in branch} for branch in output.get('branches', [])]
        output['response_scope'] = 'current_planning_with_historical_task_summaries'
    if 'next_action' not in output:
        stage_hint = _stage_next_action(state)
        if stage_hint:
            output['next_action'] = stage_hint
    return output


def _stage_next_action(state: dict) -> str | None:
    stage = state.get('stage') or ''
    if stage == 'TARGET_REVIEW':
        return ("Target identity verifies sequence and canonical cysteine count only; do not infer domain or pKa. "
                "select_site records an exploratory hypothesis; site_assessment.required_evidence is evaluated afterwards.")
    if stage in {'DESIGN_READY', 'NEEDS_POSE_EVALUATION', 'NO_GRAPH_CANDIDATES', 'DIRECT_LO_READY'}:
        return ("Use prepare_docking_input(docking_route='auto') when offered: <=6 admitted candidates route to direct CovDock-LO; "
                ">6 require VS preselection or an explicit docking_route='vs_then_lo' reason. Never invent a prior VS job.")
    if stage in {'RECEPTOR_PREPARATION_REVIEW', 'VS_PLAN_READY', 'COVDOCK_PLAN_READY'}:
        return ("Complete construct evidence review, conformer selection, and environment review before requesting native approval. "
                "A site-specific altloc audit does not establish whole-receptor readiness.")
    if stage.endswith('_RUNNING'):
        return ("A local QUEUED receipt is registration only, not remote GPU allocation; zero-GPU plans do not require GPU submission. "
                "Query status once (or check ligand_continuation) and end the turn while waiting.")
    if stage in {'VS_EXECUTION_REVIEW', 'COVDOCK_EXECUTION_REVIEW', 'POSE_EXECUTION_REVIEW'}:
        return ("validated_inhibitor=false means unvalidated, never inactive; missing/null means unmeasured, never zero. "
                "Strict CovDock v3 local strain uses r_strain_Local_Strain_Energy; structural QC pass does not establish activity.")
    return None


def outcome(state: dict) -> dict:
    """Describe only established facts; graph/branch delivery is not drug validation."""
    stage = state["stage"]
    blockers = []
    from .docking_policy import POLICY
    if stage in {"NEEDS_POSE_EVALUATION", "POSE_PLAN_READY"} and "carsidock-cov" in POLICY["disabled_engines"]:
        blockers.append({"code": "capability_not_implemented", "scope": "covdock_vs_pose_workflow",
            "reason": POLICY["reason"], "recoverable": True})
    if state.get("pending"):
        blockers.append({"code": "operation_reconciliation_required", "scope": "workflow_operation",
            "reason": "A persisted operation has no committed receipt; do not replay it.", "recoverable": True})
    if stage == "NEEDS_PARENT_PAIR_REVIEW":
        blockers.append({"code": "parent_pair_review_required", "scope": "parent_cys_pair",
            "reason": "; ".join((state.get("parent_pair_review") or {}).get("reasons", [])),
            "recoverable": True})
    if stage == "EVIDENCE_FAILED":
        blockers.append({"code": "evidence_collection_failed", "scope": "evidence_collection",
            "reason": "The recorded collection failed or was interrupted; this is not target infeasibility.", "recoverable": True})
    if stage in {"NEEDS_POSE_EVALUATION", "POSE_PLAN_READY", "POSE_INPUTS_BLOCKED"}:
        blockers.append({"code": "pose_evidence_required", "scope": "workflow_pose_refinement",
            "reason": "Execute the registered pose plan and verify candidate geometry before preparing refinement. Refinement and MD have separate evidence and approval gates.", "recoverable": True})
    assessment = {
        "NEEDS_PARENT_PAIR_REVIEW": ("parent_cys_pair", "evidence_insufficient", "Parent–Cys pairing requires review; no enumeration or docking was submitted."),
        "NO_GRAPH_CANDIDATES": ("graph_generation", "rejected", "No candidates passed this bounded graph-generation branch."),
        "DESIGN_REVIEW_FAILED": ("design_review", "rejected", "This design branch did not pass its recorded review."),
        "POSE_INPUTS_BLOCKED": ("pose_inputs", "evidence_insufficient", "Pose-input prerequisites remain unresolved."),
        "NEEDS_POSE_EVALUATION": ("candidate_pose", "not_evaluated", "Graph generation does not establish pose quality or inhibition."),
        "POSE_PLAN_READY": ("candidate_pose", "not_evaluated", "An exported pose plan is not an executed evaluation."),
        "BRANCH_REPORTED": ("drug_design_validation", "not_evaluated", "Branch delivery does not establish computational or experimental validation."),
    }.get(stage, ("drug_design_validation", "not_evaluated", "Evidence and design preparation do not establish a validated inhibitor."))
    artifacts = []
    historical = state.get('historical_md_source')
    if historical:
        artifacts.append({'kind': 'historical_md_source', 'id': historical['artifact_id'],
            'sha256': historical['manifest_sha256'], 'hash_scope': 'manifest'})
        if historical.get('evidence_error'):
            blockers.append({'code': 'evidence_rejected', 'scope': 'historical_md_source',
                'reason': historical['evidence_error'], 'recoverable': True})
        if stage == 'HISTORICAL_MD_SOURCE_REVIEW':
            assessment = ('historical_md_source', 'review_required',
                'Historical source chain is preserved; canonical scope and native graph require current verification.')
            if historical.get('md_selection_error'):
                blockers.append({'code': 'evidence_rejected', 'scope': 'historical_md_candidate',
                    'reason': historical['md_selection_error'], 'recoverable': True})
            if historical.get('md_graph_plan_error'):
                blockers.append({'code': 'evidence_rejected', 'scope': 'historical_md_graph_plan',
                    'reason': historical['md_graph_plan_error'], 'recoverable': True})
            plan = historical.get('md_graph_plan') or {}
            if plan.get('state') == 'runtime_configuration_required':
                blockers.append({'code': 'runtime_configuration_required', 'scope': 'historical_md_graph_plan',
                    'reason': plan['reason'], 'recoverable': True})
    identity = state.get("target_identity")
    if identity:
        artifacts.append({"kind": "target_identity", "id": identity["target_artifact_id"],
                          "sha256": identity["target_manifest_sha256"], "hash_scope": "manifest"})
    if state.get("evidence_id"):
        artifacts.append({"kind": "sealed_evidence_version", "id": state["evidence_id"]})
    if state.get("proposal"):
        artifacts.append({"kind": "parent_proposal", "id": state["proposal"]["proposal_id"],
                          "sha256": state["proposal"]["proposal_sha256"], "hash_scope": "artifact"})
    if state.get("design_id"):
        artifacts.append({"kind": "ligand_design", "id": state["design_id"]})
    from .ligand_receptor_source import context as receptor_context
    handoff = receptor_context(state)
    if handoff.get("target"):
        target = handoff["target"]
        artifacts.append({"kind": "refinement_target_drafts", "id": target["artifact_id"],
                          "sha256": target["manifest_sha256"], "hash_scope": "manifest"})
        prepared = handoff.get('receptor_preparation') or {}
        if prepared.get('evaluation_verification') != 'verified' or prepared.get('evidence_error'):
            blockers.append({"code": "preparation_required", "scope": "covdock_receptor_preparation",
                "reason": "CovDock requires verified receptor chemistry, excluded-environment and unresolved-residue review. The separate AF3 sequence plan enables its own data pipeline.",
                "recoverable": True})
        elif not handoff.get('environment_review'):
            blockers.append({'code': 'environment_review_required', 'scope': 'covdock_receptor_environment',
                'reason': 'Isolated protein quality was replayed; assembly, excluded components and site suitability remain to be reviewed.',
                'recoverable': True})
        elif handoff['environment_review']['decision'] != 'suitable_for_exploratory_refinement':
            blockers.append({'code': 'environment_review_required', 'scope': 'covdock_receptor_environment',
                'reason': 'The recorded agent assessment is ' + handoff['environment_review']['decision'] +
                    '; resolve its evidence gaps or revise the study before proceeding.', 'recoverable': True})
    if handoff.get("target_error"):
        blockers.append({"code": "evidence_rejected", "scope": "refinement_target_inputs",
                         "reason": handoff["target_error"]["reason"], "recoverable": True})
    if handoff.get('receptor_input'):
        receptor = handoff['receptor_input']
        artifacts.append({'kind': 'study_receptor_preparation_input', 'id': receptor['artifact_id'],
                          'sha256': receptor['manifest_sha256'], 'hash_scope': 'manifest'})
    if handoff.get('receptor_input_error'):
        blockers.append({'code': 'evidence_rejected', 'scope': 'receptor_preparation_input',
                         'reason': handoff['receptor_input_error']['reason'], 'recoverable': True})
    preparation = handoff.get('receptor_preparation') or {}
    if preparation.get('runtime_error'):
        blockers.append({'code': 'runtime_configuration_required', 'scope': 'receptor_preparation_runtime',
                         'reason': preparation['runtime_error'], 'recoverable': True})
    covdock_input = handoff.get('covdock_input')
    if covdock_input:
        artifacts.append({'kind': 'covdock_planning_input', 'id': covdock_input['artifact_id'],
                          'sha256': covdock_input['manifest_sha256'], 'hash_scope': 'manifest'})
    if handoff.get('covdock_input_error'):
        blockers.append({'code': 'evidence_rejected', 'scope': 'covdock_inputs',
                         'reason': handoff['covdock_input_error']['reason'], 'recoverable': True})
    if handoff.get('environment_review'):
        review = handoff['environment_review']
        artifacts.append({'kind': 'agent_environment_assessment', 'id': review['artifact_id'],
                          'sha256': review['manifest_sha256'], 'hash_scope': 'manifest'})
    if handoff.get('environment_review_error'):
        blockers.append({'code': 'evidence_rejected', 'scope': 'covdock_environment_review',
                         'reason': handoff['environment_review_error'], 'recoverable': True})
    if (handoff.get('covdock_plan') or {}).get('runtime_error'):
        blockers.append({'code': 'runtime_configuration_required', 'scope': 'covdock_runtime',
                         'reason': handoff['covdock_plan']['runtime_error'], 'recoverable': True})
    if state.get("pose_report"):
        report = state["pose_report"]
        artifacts.append({"kind": "exploratory_pose_report", "id": report["artifact_id"],
                          "sha256": report["manifest_sha256"], "hash_scope": "manifest"})
    record = state.get("pose_execution") or {}
    execution_state = {"QUEUED": "submitted", "RUNNING": "running", "COMPLETED": "completed",
                       "FAILED": "failed", "CANCELLED": "failed", "TIMED_OUT": "failed"}.get(record.get("job_state"), "not_submitted")
    if state.get("pending") and state["pending"].get("action") == "start_pose":
        execution_state = "outcome_unknown"
    if state["stage"] in {"POSE_EXECUTION_RUNNING", "POSE_EXECUTION_REVIEW"}:
        assessment = ("candidate_pose", "not_evaluated", "Registered execution state is not scientific pose acceptance.")
        if record.get("evaluation_verification") == "verified":
            evaluation = record["evaluation"]
            assessment = ("mapped_product_graph_and_pose_geometry",
                          "review_required" if evaluation["passing_candidate_count"] else
                          "evidence_insufficient" if evaluation["incomplete_candidate_count"] else "rejected",
                          "Independent geometry gates were recomputed; this is not inhibitor or MD acceptance.")
        elif record.get("evaluation_verification") == "evidence_rejected":
            assessment = ("candidate_pose", "evidence_insufficient", "Registered pose evidence failed revalidation.")
        if (record.get("evaluation_verification") != "verified"
                or not (record.get("evaluation") or {}).get("passing_candidate_count")):
            blockers.append({"code": "pose_evidence_required", "scope": "workflow_pose_refinement",
                             "reason": "Refinement requires independently verified geometry-passing candidates from the registered pose task; MD requires its own downstream evidence and approval.", "recoverable": True})
    if stage.startswith('AF3_') or (state.get('pending') or {}).get('action') == 'start_af3':
        record = state.get('af3_execution') or {}
        execution_state = {'QUEUED': 'submitted', 'RUNNING': 'running', 'COMPLETED': 'completed',
            'FAILED': 'failed', 'CANCELLED': 'failed', 'TIMED_OUT': 'failed'}.get(record.get('job_state'), 'not_submitted')
        if (state.get('pending') or {}).get('action') == 'start_af3':
            execution_state = 'outcome_unknown'
        error = record.get('input_error') or record.get('evidence_error')
        assessment = ('refinement_outputs', 'evidence_insufficient' if error else 'not_evaluated',
            error or 'AF3 execution state does not establish pose, affinity, inhibition or MD acceptance.')
        if not error and record.get('evaluation_verification') == 'verified':
            evaluation = record['evaluation']
            assessment = (evaluation['assessment_scope'], evaluation['assessment'],
                'Registered AF3 confidence and limited geometry were remeasured; full physical, inhibitor and MD acceptance remain unestablished.')
            if record.get('report'):
                report = record['report']
                artifacts.append({'kind': 'af3_measurement_report', 'id': report['artifact_id'],
                                  'sha256': report['manifest_sha256'], 'hash_scope': 'manifest'})
        if record.get('runtime_error'):
            blockers.append({'code': 'runtime_configuration_required', 'scope': 'af3_runtime',
                'reason': record['runtime_error'], 'recoverable': True})
        blockers.append({'code': 'capability_not_implemented', 'scope': 'full_refinement_acceptance_and_covdock',
            'reason': 'AF3 measurement readback is available; full physical acceptance, prepared-receptor CovDock and shared MD handoff remain incomplete.',
            'recoverable': False})
    if stage.startswith('RECEPTOR_PREPARATION_') or (state.get('pending') or {}).get('action') == 'start_receptor_preparation':
        record = handoff.get('receptor_preparation') or {}
        execution_state = {'QUEUED': 'submitted', 'RUNNING': 'running', 'COMPLETED': 'completed',
            'FAILED': 'failed', 'CANCELLED': 'failed', 'TIMED_OUT': 'failed'}.get(record.get('job_state'), 'not_submitted')
        if (state.get('pending') or {}).get('action') == 'start_receptor_preparation':
            execution_state = 'outcome_unknown'
        assessment = ('prepared_receptor', 'evidence_insufficient' if record.get('evidence_error') else 'not_evaluated',
            record.get('evidence_error') or 'CPU task completion is not receptor quality, environment or CovDock acceptance.')
        if execution_state == 'failed' and not record.get('evidence_error'):
            assessment = ('receptor_preparation_failure', 'review_required',
                'The registered preparation failed. Inspect its bound failure diagnostics; '
                'this does not establish molecular inactivity or authorize another computation.')
            blockers.append({'code': 'receptor_preparation_failed', 'scope': 'prepared_receptor',
                'reason': 'No prepared receptor was admitted. Preserve the original task and review its diagnostics; '
                          'workflow reconsider can archive this branch for a justified source revision.',
                'recoverable': True})
        if not record.get('evidence_error') and record.get('evaluation_verification') == 'verified':
            assessment = (record['evaluation']['assessment_scope'], 'review_required',
                'Isolated-receptor identity and geometry were replayed; descriptive context is not environment suitability or CovDock/MD admission.')
            if record.get('report'):
                report = record['report']
                artifacts.append({'kind': 'receptor_quality_and_context_report', 'id': report['artifact_id'],
                                  'sha256': report['manifest_sha256'], 'hash_scope': 'manifest'})
        if handoff.get('environment_review_error'):
            assessment = ('agent_environment_assessment', 'evidence_insufficient',
                          handoff['environment_review_error'])
        elif handoff.get('environment_review'):
            decision = handoff['environment_review']['decision']
            assessment = ('agent_environment_assessment',
                {'suitable_for_exploratory_refinement': 'review_required',
                 'requires_revision': 'rejected', 'evidence_insufficient': 'evidence_insufficient'}[decision],
                'Recorded agent judgment: ' + decision + '. This concerns the current receptor/plan, '
                'not molecular inactivity, validated biology or compute authorization.')
    if stage.startswith('COVDOCK_EXECUTION_') or (state.get('pending') or {}).get('action') == 'start_covdock':
        record = state.get('covdock_execution') or {}
        execution_state = {'QUEUED': 'submitted', 'RUNNING': 'running', 'COMPLETED': 'completed',
            'FAILED': 'failed', 'CANCELLED': 'failed', 'TIMED_OUT': 'failed'}.get(record.get('job_state'),
                'outcome_unknown' if record.get('run_id') else 'not_submitted')
        if (state.get('pending') or {}).get('action') == 'start_covdock':
            execution_state = 'outcome_unknown'
        assessment = ('covdock_registered_outputs', 'evidence_insufficient' if record.get('evidence_error') else 'not_evaluated',
            record.get('evidence_error') or 'CovDock task completion is not structural acceptance, inhibition evidence or MD admission.')
        if record.get('evaluation_verification') == 'verified':
            value = record['evaluation']
            assessment = (value['assessment_scope'], value['assessment'],
                ('Registered structures and frozen provisional gates were rechecked; full chemical validity, inhibition and MD readiness remain unestablished.'
                 if value['measured_candidate_count'] else
                 'The registered batch and frozen gates were checked, but no successful structures are available; this is insufficient evidence, not molecular inactivity.'))
            if record.get('report'):
                report = record['report']
                artifacts.append({'kind': 'covdock_measurement_report', 'id': report['artifact_id'],
                                  'sha256': report['manifest_sha256'], 'hash_scope': 'manifest'})
            if record.get('md_handoff'):
                delivery = record['md_handoff']
                artifacts.append({'kind': 'shared_md_candidate_delivery', 'id': delivery['artifact_id'],
                                  'sha256': delivery['manifest_sha256'], 'hash_scope': 'manifest'})
        if record.get('readback_wait'):
            assessment = ('covdock_registered_outputs', 'evidence_insufficient', record['readback_wait'])
            blockers.append({'code': 'runtime_configuration_required', 'scope': 'covdock_structure_readback',
                             'reason': record['readback_wait'], 'recoverable': True})
        if record.get('md_handoff_error'):
            blockers.append({'code': 'evidence_rejected', 'scope': 'md_candidate_delivery',
                             'reason': record['md_handoff_error'], 'recoverable': True})
        if record.get('md_selection_verification') == 'verified':
            selected = record['md_selection']
            artifacts.append({'kind': 'candidate_pose_input', 'id': selected['artifact_id'],
                              'sha256': selected['manifest_sha256'], 'hash_scope': 'manifest'})
        if record.get('md_selection_error'):
            blockers.append({'code': 'evidence_rejected', 'scope': 'md_candidate_selection',
                             'reason': record['md_selection_error'], 'recoverable': True})
        graph_plan = record.get('md_graph_plan') or {}
        if record.get('md_graph_plan_error'):
            blockers.append({'code': 'evidence_rejected', 'scope': 'native_graph_plan',
                             'reason': record['md_graph_plan_error'], 'recoverable': True})
        if graph_plan.get('state') == 'runtime_configuration_required':
            blockers.append({'code': 'runtime_configuration_required', 'scope': 'native_covalent_graph_check',
                             'reason': graph_plan['reason'], 'recoverable': True})
        blockers.append({'code': 'capability_not_implemented', 'scope': 'full_refinement_acceptance_and_md',
            'reason': 'Current-study candidate delivery, pose selection and native graph planning are available; native graph execution/readback requires a separately approved frozen plan, and full MD admission/execution remains unfinished.',
            'recoverable': False})
    if state.get('md_graph_execution'):
        from .ligand_md_graph_execution import projection
        record = projection(state)
        execution_state = {'QUEUED': 'submitted', 'RUNNING': 'running', 'COMPLETED': 'completed',
            'FAILED': 'failed', 'CANCELLED': 'failed', 'TIMED_OUT': 'failed'}.get(record.get('job_state'), 'outcome_unknown')
        if state.get('pending') or not record.get('job_identity_verified'):
            execution_state = 'outcome_unknown'
        assessment = ('native_covalent_graph_check', 'not_evaluated',
            'Task state alone does not verify a covalent graph or MD readiness.')
        if record.get('verification') == 'verified':
            assessment = ('native_covalent_graph_check', 'review_required',
                'Registered native graph evidence was replayed; parameterization and full MD admission remain pending.')
            receipt = record['admission']
            artifacts.append({'kind': 'native_graph_admission_receipt', 'id': receipt['run_id'],
                              'sha256': receipt['manifest_sha256'], 'hash_scope': 'manifest'})
        if record.get('evidence_error'):
            assessment = ('native_covalent_graph_check', 'evidence_insufficient', record['evidence_error'])
            blockers.append({'code': 'evidence_rejected', 'scope': 'native_covalent_graph_check',
                             'reason': record['evidence_error'], 'recoverable': True})
        blockers.append({'code': 'capability_not_implemented', 'scope': 'full_md_workflow',
            'reason': 'Graph checking, parameterization, preparation, short QC and equilibration are connected; production MD and analysis remain unconnected.',
            'recoverable': False})
    if state.get('md_parameterization'):
        from .ligand_md_parameterization import projection as parameterization_projection
        record = parameterization_projection(state)
        execution_state = {'QUEUED': 'submitted', 'RUNNING': 'running', 'COMPLETED': 'completed',
            'FAILED': 'failed', 'CANCELLED': 'failed', 'TIMED_OUT': 'failed'}.get(record.get('job_state'), 'not_submitted')
        if state.get('pending') or record.get('run_id') and not record.get('job_identity_verified'):
            execution_state = 'outcome_unknown'
        assessment = ('native_covalent_parameterization', 'not_evaluated', 'Parameterization has not been verified.')
        if record.get('parameterization_completed'):
            witness = record['parameterization']
            assessment = ('native_covalent_parameterization', 'review_required',
                'Registered parameters bind the original candidate and graph; system preparation and MD remain pending.')
            artifacts.append({'kind': 'native_parameterization_receipt', 'id': witness['run_id'],
                              'sha256': witness['manifest_sha256'], 'hash_scope': 'manifest'})
        if record.get('evidence_error'):
            assessment = ('native_covalent_parameterization', 'evidence_insufficient', record['evidence_error'])
            blockers.append({'code': 'evidence_rejected', 'scope': 'native_covalent_parameterization',
                             'reason': record['evidence_error'], 'recoverable': True})
        plan = record.get('plan') or {}
        if plan.get('state') == 'runtime_configuration_required':
            blockers.append({'code': 'runtime_configuration_required', 'scope': 'native_covalent_parameterization',
                             'reason': plan['reason'], 'recoverable': True})
    for name in md_preparation.ENGINES:
        if not state.get('md_' + name):
            continue
        record = md_preparation.projection(state, name)
        scope = 'openmm_' + name
        execution_state = {'QUEUED': 'submitted', 'RUNNING': 'running', 'COMPLETED': 'completed',
            'FAILED': 'failed', 'CANCELLED': 'failed', 'TIMED_OUT': 'failed'}.get(record.get('job_state'), 'not_submitted')
        if state.get('pending') or record.get('run_id') and not record.get('job_identity_verified'):
            execution_state = 'outcome_unknown'
        assessment = (scope, 'not_evaluated', 'The original registered stage has not been verified.')
        if record.get('verification') == 'verified':
            witness = record[name]
            reason = ('Conversion, forcefield audit and bounded minimization verified; short QC is pending.'
                      if name == 'system_preparation' else
                      'Short-QC integration stability verified; eligible for equilibration planning review only. '
                      'Equilibration has not run; its independent plan may now be prepared.')
            assessment = (scope, 'review_required', reason)
            if name == 'equilibration':
                assessment = (scope, witness['assessment'],
                    'NPT protocol and registered outputs verified; stability checks passed. Eligible for production planning review only.'
                    if witness['production_review_ready'] else
                    'NPT protocol completed but stability checks failed. Production planning is not admitted; review the failed checks.')
                if not witness['production_review_ready']:
                    blockers.append({'code': 'equilibration_stability_failed', 'scope': scope,
                        'reason': 'Completed protocol does not meet the frozen stability checks.', 'recoverable': False})
            artifacts.append({'kind': scope + '_receipt', 'id': witness['run_id'],
                              'sha256': witness['manifest_sha256'], 'hash_scope': 'manifest'})
        if record.get('evidence_error'):
            assessment = (scope, 'evidence_insufficient', record['evidence_error'])
            blockers.append({'code': 'evidence_rejected', 'scope': scope,
                             'reason': record['evidence_error'], 'recoverable': True})
        elif execution_state == 'failed':
            blockers.append({'code': record.get('failure_reason') or 'job_failed', 'scope': scope,
                             'reason': record.get('message') or 'The registered stage did not complete.', 'recoverable': False})
    if (state.get('vs_input') and not state.get('vs_execution')
            and not stage.startswith('RECEPTOR_PREPARATION_')
            and (state.get('pending') or {}).get('action') != 'start_receptor_preparation'):
        blockers = [row for row in blockers if row['scope'] != 'workflow_pose_refinement']
        record, execution_state = {}, 'not_submitted'
        assessment = ('covdock_vs_environment', 'review_required',
            'Current VS inputs are bound; receptor environment judgment and native execution are separate steps.')
        reviewed = state.get('vs_environment_review') or {}
        if reviewed.get('decision') in {'requires_revision', 'evidence_insufficient'}:
            assessment = ('covdock_vs_environment', 'evidence_insufficient',
                'The current source-bound VS environment judgment does not support execution.')
        error = state.get('vs_input_error') or state.get('vs_review_error')
        if error:
            assessment = ('covdock_vs_environment', 'evidence_insufficient', error)
            blockers.append({'code':'evidence_rejected', 'scope':'covdock_vs_environment',
                'reason':error, 'recoverable':True})
        if not POLICY['native_vs_workflow_ready'] and not any(
                row['scope'] == 'covdock_vs_pose_workflow' for row in blockers):
            blockers.append({'code':'capability_not_implemented', 'scope':'covdock_vs_pose_workflow',
                'reason':'VS planning is connected; independent output review and release integration remain incomplete.',
                'recoverable':True})
    if state.get('vs_execution') and state['stage'] in {'VS_EXECUTION_RUNNING', 'VS_EXECUTION_REVIEW', 'VS_REFINEMENT_READY'}:
        record = state['vs_execution']
        execution_state = {'QUEUED':'submitted', 'RUNNING':'running', 'COMPLETED':'completed',
            'FAILED':'failed', 'CANCELLED':'failed', 'TIMED_OUT':'failed'}.get(record.get('job_state'), 'outcome_unknown')
        assessment = ('covdock_vs_structures', 'not_evaluated',
            'VS task status is not independent output validation, scientific acceptance or MD admission.')
        summary = record.get('evaluation_summary')
        if summary:
            accepted = summary['passing_candidate_count'] > 0
            complete = all(item['evidence_complete'] for item in summary['candidates'])
            assessment = ('provisional_vs_geometry', 'passed' if accepted else 'rejected' if complete else 'evidence_insufficient',
                'Raw product identity and provisional geometry gates were independently replayed; native MD admission remains pending.')
            ref = record['evaluation_artifact']
            artifacts.append({'kind':'vs_geometry_review','id':ref['artifact_id'],
                'sha256':ref['manifest_sha256'],'hash_scope':'artifact'})
        if record.get('evidence_error'):
            assessment = ('covdock_vs_structures', 'evidence_insufficient', record['evidence_error'])
            blockers.append({'code':'evidence_rejected', 'scope':'covdock_vs_structures',
                'reason':record['evidence_error'], 'recoverable':True})
    refinement_error = (state.get('refinement_input') or {}).get('source_error')
    if refinement_error:
        scope = ('direct_lo_source' if (state.get('refinement_input') or {}).get('source_kind')
                 == 'sealed-ligand-direct-lo-v1' else 'vs_refinement_source')
        assessment = (scope, 'evidence_insufficient', refinement_error)
        blockers.append({'code': 'evidence_rejected', 'scope': scope,
            'reason': refinement_error, 'recoverable': True})
    return {"execution": {"scope": "registered_scientific_compute", "state": execution_state,
                          "task_ids": [record["run_id"]] if record.get("run_id") else []},
            "assessment": dict(zip(("scope", "state", "reason"), assessment)),
            "artifacts": artifacts, "blockers": blockers}


def start(root: Path, *, request_id: str, target: str, taxon: int, site: str | None = None,
          evidence_id: str | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", request_id):
        raise ValueError("A stable request_id is required")
    if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,39}", target)
        or (site is not None and not re.fullmatch(r"C[1-9][0-9]{0,4}", site)) or type(taxon) is not int or taxon < 1):
        raise ValueError("Use an explicit gene/species and optionally a canonical Cys site")
    if evidence_id and site is None:
        raise ValueError("Resuming site-bound evidence requires its explicit site")
    identifier = "ligand-flow-" + sha256(request_id.encode()).hexdigest()[:24]
    folder = directory(root, identifier)
    folder.mkdir(parents=True, exist_ok=True)
    scope = {"target": target, "site": site, "taxon": taxon}
    with version_lock(folder, ".workflow.lock"):
        if (folder / "state.json").exists():
            state = load(folder)
            if state.get("initial_target", state["target"]) != scope or state["initial_evidence_id"] != evidence_id:
                raise ValueError("request_id already binds different scientific inputs")
            refresh(root, folder, state)
            return response(state)
        state = {"workflow_id": identifier, "revision": 0, "target": scope, "initial_target": dict(scope), "request_id": request_id,
            "initial_evidence_id": evidence_id, "evidence_id": evidence_id, "proposal": None,
            "design_id": None, "stage": "TARGET_REVIEW" if site is None else "INTAKE",
            "target_identity": None, "site_selection": None,
            "branches": [], "events": [], "pending": None}
        if evidence_id:
            current = evidence_tools.read(root, evidence_id)
            if current["state"] != "EVIDENCE_READY":
                raise ValueError("Only committed evidence can be explicitly resumed")
            require_scope(state, current["evidence"])
            if latest_version(root, evidence_id) != evidence_id:
                raise ValueError("Resume using the latest evidence version")
            state["stage"] = "PARENT_REVIEW"
        changed(folder, state, "start", {"reused_evidence": bool(evidence_id)})
        return response(state)


def navigate(root: Path, action: str, **arguments) -> dict:
    if action == 'attach_md_source':
        from .ligand_historical_md_source import attach
        return attach(root, **arguments)
    if action == "reconcile":
        return reconcile(root, **arguments)
    if action == "start":
        return start(root, **arguments)
    if action not in {"status", "resume", "suspend", "reconsider", "reopen_design", "inspect_branch", "select_site", "revise_target"}:
        raise ValueError("Unknown workflow navigation action")
    allowed = {"workflow_id"} if action in {"status", "resume"} else {"workflow_id", "expected_revision"} | {
        "inspect_branch": {"branch_index"}, "select_site": {"site", "reason"},
        "revise_target": {"target", "taxon", "reason"}, "reconsider": {"reason"},
        "reopen_design": {"reason"}, "suspend": {"reason"}}[action]
    if set(arguments) != allowed:
        missing = sorted(allowed - set(arguments))
        unexpected = sorted(set(arguments) - allowed)
        raise ValueError(
            f"Invalid navigation arguments for {action}: "
            f"missing={missing}; unexpected={unexpected}. "
            "Supply the named missing fields; do not guess branch_index or change the design."
        )
    folder = directory(root, arguments["workflow_id"])
    with version_lock(folder, ".workflow.lock"):
        state = load(folder)
        from .ligand_receptor_execution import RUN_STAGES, read_projection, reject_evidence
        if action in {'status', 'resume'} and state['stage'] in RUN_STAGES:
            try:
                read_projection(root, state)
            except (ValueError, KeyError, TypeError, OSError) as error:
                reject_evidence(state, error)
            from .ligand_covdock_input import verify_saved_readonly
            verify_saved_readonly(root, state)
            return response(state)
        refresh(root, folder, state)
        if action in {"status", "resume"}:
            return response(state)
        revision(state, arguments["expected_revision"])
        if action == "inspect_branch":
            index = arguments["branch_index"]
            if type(index) is not int or not 0 <= index < len(state["branches"]):
                raise ValueError("Select an actual zero-based branch_index from status")
            previous = state["branches"][index]
            detail = design_tools.inspect(root, previous["design_id"]) if previous["design_id"] else previous
            return response(state, {"historical_branch": True, "branch_index": index, "record": detail,
                "active_handoff_restored": False})
        reason = arguments["reason"]
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000:
            raise ValueError("Record a concrete reconsideration reason (5-2000 characters)")
        if state.get("pending"):
            raise ValueError("Uncertain operation requires reconciliation; do not repeat or reselect")
        for name in md_preparation.ENGINES:
            record = state.get('md_' + name) or {}
            if record.get('approved_scope') and action != 'suspend' and (not record.get('job_identity_verified')
                    or record.get('job_state') not in md_preparation.TERMINAL):
                raise ValueError('Observe the original MD execution before reconsidering')
        parameterization = state.get('md_parameterization') or {}
        if parameterization.get('approved_scope') and action != 'suspend' and (not parameterization.get('job_identity_verified')
                or parameterization.get('job_state') not in {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}):
            raise ValueError('Observe the original parameterization execution before reconsidering')
        graph = state.get('md_graph_execution') or {}
        if graph and action != 'suspend' and (not graph.get('job_identity_verified') or graph.get('job_state') not in {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}):
            raise ValueError('Observe the original graph execution before reconsidering')
        if state["stage"] in {"VS_EXECUTION_RUNNING", "POSE_EXECUTION_RUNNING", "AF3_EXECUTION_RUNNING", "RECEPTOR_PREPARATION_RUNNING", "COVDOCK_EXECUTION_RUNNING", "MD_GRAPH_RUNNING", "MD_PARAMETERIZATION_RUNNING", "MD_SYSTEM_PREPARATION_RUNNING", "MD_SHORT_QC_RUNNING", "MD_EQUILIBRATION_RUNNING"} and action != "suspend":
            raise ValueError("Observe the current pose execution before reconsidering")
        if action == "reopen_design":
            if state["stage"] != "BRANCH_REPORTED" or not state.get("design_id"):
                raise ValueError("Only an existing reported design can be reopened")
            record = state.get("pose_execution") or {}
            if record.get("run_id") or record.get("approved_scope"):
                raise ValueError("Observe or reconcile the existing execution; do not reopen it")
            current = design_tools.inspect(root, state["design_id"])
            if current["evidence_id"] != state["evidence_id"]:
                raise ValueError("Reported design belongs to different evidence")
            if current["state"] not in {"DESIGN_INPUTS_READY", "NEEDS_POSE_EVALUATION", "POSE_PLAN_READY"}:
                raise ValueError("No resumable design stage; reconsider the parent instead")
            state.update(stage=current["state"], pose_execution=None)
            changed(folder, state, action, {"reason": reason, "previous_pose_plan": record})
            return response(state, {"design_id": state["design_id"], "design_reused": True,
                "runtime_recheck_required": True, "compute_submitted": False})
        if action == "suspend":
            changed(folder, state, action, {"reason": reason})
            return response(state, {"session_binding": "suspended", "workflow_preserved": True,
                "jobs_cancelled": False, "guidance": "The host may now expose another route; resume restores this workflow. Running jobs are not cancelled."})
        if action in {"select_site", "revise_target"}:
            if action not in view(state)["tools"][ENTRY]["actions"]:
                raise ValueError("Target/site selection is unavailable at this workflow stage")
            if action == "revise_target":
                from .target_identity import target_plan
                target_plan(arguments["target"], arguments["taxon"])
                previous = {"target": dict(state["target"]), "target_identity": state.get("target_identity")}
                state.update(target={"target": arguments["target"], "taxon": arguments["taxon"], "site": None},
                             target_identity=None)
                changed(folder, state, action, {"reason": reason, "previous": previous})
                return response(state)
            identity = read_target(root, state)
            site = arguments["site"]
            if not isinstance(site, str) or not re.fullmatch(r"C[1-9][0-9]{0,4}", site):
                raise ValueError("Select an explicit canonical cysteine")
            if int(site[1:]) not in identity["identity"]["candidates"][0]["canonical_cysteines"]:
                raise ValueError("Selected site is not a cysteine in the verified canonical sequence")
            state["target"]["site"] = site
            state["site_selection"] = {"site": site, "reason": reason,
                "target_identity": dict(state["target_identity"]), "interpretation": "exploratory_hypothesis",
                "reactivity_validated": False}
            state["stage"] = "INTAKE"
            changed(folder, state, action, state["site_selection"])
            return response(state)
        if state["stage"] == "EVIDENCE_PENDING":
            raise ValueError("Observe the existing collection before reconsidering")
        archive(state, reason)
        if state["stage"] == "EVIDENCE_FAILED":
            state["events"].append({"operation": "retain_failed_collection", "evidence_id": state["evidence_id"]})
            state["evidence_id"] = None
        state["stage"] = ("PARENT_REVIEW" if state.get("evidence_id") else
                          "INTAKE" if state["target"]["site"] else "TARGET_REVIEW")
        changed(folder, state, "reconsider", {"reason": reason})
        return response(state)


def step(root: Path, tool: str, *, workflow_id: str, expected_revision: int, action: str, **arguments) -> dict:
    if tool == DESIGN and action == 'prepare_docking_input':
        from .ligand_docking import prepare
        return prepare(root, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    from .ligand_direct_lo import ACTIONS as DIRECT_LO_ACTIONS, lifecycle as direct_lo
    if tool == DESIGN and action in DIRECT_LO_ACTIONS:
        return direct_lo(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    from .ligand_vs_refinement import ACTIONS as VS_REFINEMENT_ACTIONS, lifecycle as vs_refinement
    if tool == DESIGN and action in VS_REFINEMENT_ACTIONS:
        return vs_refinement(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    from .ligand_vs_input import ACTIONS as VS_ACTIONS, lifecycle as vs_lifecycle
    if tool == DESIGN and action in VS_ACTIONS:
        return vs_lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    from .ligand_vs_execution import ACTIONS as VS_EXECUTION_ACTIONS, lifecycle as vs_execution
    if tool == DESIGN and action in VS_EXECUTION_ACTIONS:
        return vs_execution(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action == 'prepare_historical_md_candidate':
        from .ligand_historical_md_source import prepare_candidate
        return prepare_candidate(root, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in md_preparation.ACTIONS:
        return md_preparation.lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in {'plan_md_parameterization', 'start_md_parameterization',
                                    'md_parameterization_status', 'release_md_parameterization_plan'}:
        from .ligand_md_parameterization import lifecycle
        return lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in {'start_md_graph', 'md_graph_status'}:
        from .ligand_md_graph_execution import lifecycle
        return lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in {'select_md_candidate', 'release_md_candidate', 'check_md_graph_runtime'}:
        from .ligand_md_selection import lifecycle
        return lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in {'start_covdock', 'covdock_status', 'evaluate_covdock', 'report_covdock', 'prepare_md_handoff'}:
        from .ligand_covdock_execution import lifecycle
        return lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in {'prepare_covdock_input', 'check_covdock_runtime',
                                    'review_covdock_environment', 'read_covdock_environment'}:
        from .ligand_covdock_input import lifecycle
        return lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in {'start_receptor_preparation', 'receptor_preparation_status', 'refresh_receptor_preparation', 'evaluate_receptor', 'report_receptor'}:
        from .ligand_receptor_execution import lifecycle
        return lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action == 'use_prepared_receptor_for_vs':
        from .ligand_vs_receptor import lifecycle
        return lifecycle(root, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in {'read_receptor_motion', 'review_receptor_motion'}:
        from .ligand_receptor_execution import motion_review_lifecycle
        return motion_review_lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in {'select_restrained_receptor_policy', 'select_local_receptor_policy', 'select_bounded_local_receptor_policy', 'select_block_local_receptor_policy'}:
        from .ligand_receptor_preparation import select_restrained_after_failure
        return select_restrained_after_failure(root, workflow_id=workflow_id, expected_revision=expected_revision, local=action in {'select_local_receptor_policy', 'select_bounded_local_receptor_policy', 'select_block_local_receptor_policy'}, bounded=action == 'select_bounded_local_receptor_policy', block=action == 'select_block_local_receptor_policy', **arguments)
    if tool == DESIGN and action in {'prepare_receptor_input', 'check_receptor_runtime'}:
        from .ligand_receptor_preparation import lifecycle
        return lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in {'check_af3_runtime', 'start_af3', 'af3_status', 'evaluate_af3', 'report_af3'}:
        from .ligand_af3_execution import lifecycle
        return lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    if tool == DESIGN and action in {"start_pose", "pose_status", "evaluate_pose", "prepare_refinement", "prepare_refinement_target", "report_pose"}:
        from .ligand_pose_execution import lifecycle
        return lifecycle(root, action, workflow_id=workflow_id, expected_revision=expected_revision, **arguments)
    folder = directory(root, workflow_id)
    with version_lock(folder, ".workflow.lock"):
        state = load(folder)
        refresh(root, folder, state)
        revision(state, expected_revision)
        exposed = view(state)["tools"].get(tool)
        if not exposed or action not in exposed["actions"]:
            raise ValueError("Tool/action is unavailable at this workflow stage; read status or reconsider")
        if tool == EVIDENCE:
            if set(arguments) - set(FIELDS[action]):
                raise ValueError(f"Workflow binds scientific identity; unexpected evidence arguments for {action}. "
                                 f"Allowed action fields: {', '.join(FIELDS[action]) or '(none)'}. "
                                 "Do not supply workflow identity or revision; the host binds them.")
            if action == "finalize":
                hypothesis = evidence_tools.Hypothesis.model_validate(arguments["hypothesis"])
                if hypothesis.route != "ligand-first":
                    raise ValueError("This workflow only accepts Ligand-first hypotheses")
        elif arguments:
            raise ValueError("Design inputs come from the verified workflow, not model-supplied IDs/paths")
        mutates = tool == DESIGN or action not in READS | {"read_target"}
        previous_stage = state["stage"]
        if mutates:
            recovery = ({"kind": "ligand-evidence-collection-v1", "evidence_id": "hyp-" + uuid4().hex[:24],
                         "target": dict(state["target"])} if tool == EVIDENCE and action == "collect" else None)
            begin_operation(folder, state, tool, action, recovery=recovery)
        try:
            result = execute_step(root, state, tool, action, arguments)
        except Exception as error:
            if mutates:
                incident = {**state["pending"], "error": f"{type(error).__name__}: {error}"}
                # Collection spawns a worker, so a lost receipt must not replay.
                # Other adapters only publish immutable local artifacts/evidence;
                # retaining the failure permits corrected science inputs. Any
                # newly committed evidence is reconciled by refresh before reuse.
                state["pending"] = incident if action == "collect" else None
                changed(folder, state, "operation_uncertain" if state["pending"] else "tool_failed", incident)
            raise
        state["pending"] = None
        # Reading sealed sources does not change scientific state or invalidate
        # another read/finalization. A read that observes collection completion
        # does advance the stage and therefore the workflow revision.
        if mutates or state["stage"] != previous_stage:
            changed(folder, state, action, {"tool": tool, "result_state": result.get("state")})
        return response(state, result)


def execute_step(root: Path, state: dict, tool: str, action: str, arguments: dict) -> dict:
    if tool == EVIDENCE:
        if action == "search_parent_index":
            from .ligand_parent_index import search
            if state.get("target_identity"):
                accession = read_target(root, state)["identity"]["unique_verified_target"]
            else:
                evidence = evidence_tools.read(root, state["evidence_id"])
                if evidence["state"] != "EVIDENCE_READY":
                    raise ValueError("Verified target evidence is required before index lookup")
                accession = evidence["evidence"]["identity"]["uniprot_id"]
            result = search(accession, **arguments)
            result["requested_site"] = state["target"].get("site")
            result["site_filter_applied"] = False
            return result
        if action == "export_covdock_report":
            from .ligand_covdock_report import export_report
            return export_report(root, state)
        if action == "export_design_report":
            from .ligand_design_delivery import export_design_report
            return export_design_report(root, state, arguments.get("design_id"))
        if action == "export_parent_report":
            from .ligand_parent_delivery import export_parent_report
            return export_parent_report(root, state)
        if action == "read_target":
            return read_target(root, state)
        if action == "resolve_target":
            from .tool_api import library_first_resolve_target_tool
            if arguments:
                if set(arguments) != {"target_artifact_id", "target_manifest_sha256"}:
                    raise ValueError("Reuse requires both a target artifact ID and its manifest SHA256")
                reference = dict(arguments)
                result = {"reused_target_identity": True}
            else:
                result = library_first_resolve_target_tool(root, state["target"]["target"],
                    state["target"]["taxon"], action="search")
                if result["resolution"]["status"] != "identity_resolved_site_assessment_required":
                    return result
                reference = {"target_artifact_id": result["artifact_id"],
                             "target_manifest_sha256": result["manifest_sha256"]}
            verified = read_target(root, state, reference)
            state["target_identity"] = reference
            return {**result, "verified_identity": verified, "reactivity_validated": False}
        bound = state["target"] if action == "collect" else {"evidence_id": state["evidence_id"]}
        result = (evidence_tools.collect(root, **bound, identifier=state["pending"]["recovery"]["evidence_id"])
                  if action == "collect" else evidence_tools.hypothesis_tool(root, action=action, **bound, **arguments))
        if action == "collect":
            state.update(evidence_id=result["evidence_id"], stage="EVIDENCE_PENDING")
        elif action in ADDITIONS:
            if result.get("evidence_id") != state["evidence_id"]:
                archive(state, "Supplemented evidence requires a fresh comparison")
                state.update(evidence_id=result["evidence_id"], stage="PARENT_REVIEW")
        elif action == "read" and state["stage"] == "EVIDENCE_PENDING":
            if result["state"] == "EVIDENCE_READY":
                state["stage"] = "PARENT_REVIEW"
            elif result["state"] in {"FAILED", "INTERRUPTED"}:
                state["stage"] = "EVIDENCE_FAILED"
        elif action == "finalize":
            handoff = result.get("design_handoff")
            if result["state"] == "PARENT_COMPARISON_REVIEW_READY" and handoff:
                state.update(proposal=handoff["arguments"], stage="DESIGN_READY")
                result["next_action"] = ("Use export_parent_report to deliver the bound official report and hypothesis "
                    "to host Results. Remote artifact paths are not session files; do not pass them to artifact.save_file. "
                    "Parent comparison readiness does not establish receptor, candidate or MD qualification.")
        return result
    if action == "check_pose_runtime":
        from .ligand_pose_execution import freeze_runtime
        return freeze_runtime(root, state)
    if action == "prepare":
        result = design_tools.prepare(root, **{k: v for k, v in state["proposal"].items() if k != "action"})
        if result.get("state") == "NEEDS_PARENT_PAIR_REVIEW":
            state.update(stage="NEEDS_PARENT_PAIR_REVIEW",
                         parent_pair_review=result["parent_cys_assessment"])
        if result.get("design_id"):
            state["design_id"] = result["design_id"]
    else:
        result = design_tools.design_tool(root, action=action, design_id=state["design_id"])
    if action == "deliver":
        state["stage"] = "BRANCH_REPORTED"
    elif result.get("design_id"):
        state["stage"] = result["state"]
    return result


def reconcile(root: Path, *, workflow_id: str, expected_revision: int) -> dict:
    folder = directory(root, workflow_id)
    with version_lock(folder, ".workflow.lock"):
        state = load(folder)
        revision(state, expected_revision)
        pending = state.get("pending") or {}
        if pending.get("action") in {"start_vs", "start_pose", "start_af3", "start_receptor_preparation", "start_covdock", "start_md_graph", "start_md_parameterization", "start_md_system_preparation", "start_md_short_qc", "start_md_equilibration"}:
            # The pose adapter takes the same lock and rechecks revision below.
            pass
        elif pending.get("action") in LOCAL_OPERATIONS.get(pending.get("tool"), set()):
            state["pending"] = None
            changed(folder, state, "local_operation_reconciled", {"interrupted_operation": pending})
            return response(state, {"operation_state": "RESTORED_COMMITTED_ASSETS", "calculation_submitted": False,
                "partial_artifacts_adopted": False, "automatic_retry": False})
        elif pending.get("tool") == EVIDENCE and pending.get("action") == "collect":
            recovery = pending.get("recovery") or {}
            if recovery.get("kind") != "ligand-evidence-collection-v1" or recovery.get("target") != state["target"]:
                raise ValueError("Uncertain collection lacks its exact saved identity; do not relaunch")
            identifier = recovery.get("evidence_id")
            if not isinstance(identifier, str):
                raise ValueError("Uncertain collection lacks its exact saved identity; do not relaunch")
            location = evidence_tools.folder(root, identifier)
            request = location / "request.json"
            if not request.exists():
                return response(state, {"operation_state": "OUTCOME_UNKNOWN", "reason": "collection_request_missing",
                    "automatic_retry": False, "calculation_submitted": False})
            recorded = json.loads(request.read_text())
            if any(recorded.get(key) != value for key, value in state["target"].items()):
                raise ValueError("Collection receipt belongs to another target/site/species")
            if not any((location / name).exists() for name in ("manifest.json", "status.json", "worker.json")):
                return response(state, {"operation_state": "OUTCOME_UNKNOWN", "reason": "collection_receipt_missing",
                    "automatic_retry": False, "calculation_submitted": False})
            result = evidence_tools.read(root, identifier)
            stage = {"RUNNING": "EVIDENCE_PENDING", "EVIDENCE_READY": "PARENT_REVIEW",
                     "FAILED": "EVIDENCE_FAILED", "INTERRUPTED": "EVIDENCE_FAILED"}.get(result["state"])
            if stage is None:
                raise ValueError("Unrecognized collection state; keep original pending identity")
            if result["state"] == "EVIDENCE_READY":
                require_scope(state, result["evidence"])
            state.update(pending=None, evidence_id=identifier, stage=stage)
            changed(folder, state, "collection_reconciled", {"interrupted_operation": pending,
                "evidence_id": identifier, "result_state": result["state"]})
            return response(state, {"operation_state": "EXISTING_COLLECTION_OBSERVED", "evidence_id": identifier,
                "collection_state": result["state"], "automatic_retry": False, "calculation_submitted": False})
        else:
            raise ValueError("No supported uncertain operation to reconcile")
    if pending.get('action') in md_preparation.STARTS:
        return md_preparation.lifecycle(root, 'reconcile', workflow_id=workflow_id, expected_revision=expected_revision)
    if pending.get('action') == 'start_md_parameterization':
        from .ligand_md_parameterization import lifecycle
    elif pending.get('action') == 'start_md_graph':
        from .ligand_md_graph_execution import lifecycle
    elif pending.get('action') == 'start_vs':
        from .ligand_vs_execution import lifecycle
    elif pending.get('action') == 'start_covdock':
        from .ligand_covdock_execution import lifecycle
    elif pending.get('action') == 'start_receptor_preparation':
        from .ligand_receptor_execution import lifecycle
    elif pending.get('action') == 'start_af3':
        from .ligand_af3_execution import lifecycle
    else:
        from .ligand_pose_execution import lifecycle
    return lifecycle(root, "reconcile", workflow_id=workflow_id, expected_revision=expected_revision)


def read_target(root: Path, state: dict, reference: dict | None = None) -> dict:
    from .tool_api import _load_target_artifact
    identity = _load_target_artifact(root, **(reference or state["target_identity"]))
    if identity["plan"]["gene"] != state["target"]["target"] or identity["plan"]["taxon"] != state["target"]["taxon"]:
        raise ValueError("Target identity belongs to another gene/species")
    return identity
