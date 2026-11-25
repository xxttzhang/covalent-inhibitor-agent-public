"""Canonical action/field vocabulary shared by Library workflow and MCP.

This describes the complete vocabulary, not permission or stage availability.
workflow.view remains the authority for which actions are currently allowed.
"""
from . import md_execution

RECEPTOR_ACTIONS = ("plan", "start", "status", "assess_context")

FIELDS = {
    "resolve_target": ["target_artifact_id", "target_manifest_sha256"], "read_target": [],
    "discover_structures": ["limit", "discovery_artifact_id", "discovery_manifest_sha256"], "read_structures": [],
    "acquire_structure": ["entry", "label_chain", "model", "altloc", "receptor_input_artifact_id", "manifest_sha256"],
    "read_structure": [], "plan": ["altlocs", "heavy_seed", "max_missing_residues", "resource_budget"],
    "start": [], "status": [], "assess_context": [], "review_receptor": [], "read_screening_receptor": [],
    "screening_profiles": [], "prepare_screening_inputs": ["profile"], "read_screening_inputs": [],
    "prepare_screening_plan": ["scientific_review", "max_points"], "read_screening_plan": [],
    "compile_screening": [], "recover_screening": [], "approve_screening": [], "screening_status": [],
    "cancel_screening": [], "wake_screening": [], "release_screening": ["reason"], "migrate_screening_to_vs": ["reason"],
    "plan_vs_prescreen": ["scientific_review"], "start_vs_prescreen": [], "vs_prescreen_status": [], "release_vs_prescreen": ["reason"],
    "evaluate_vs_prescreen": [],
    "prepare_vs_input": [], "read_vs_input": ["evidence_path", "evidence_locator", "evidence_offset"], "review_vs_environment": ["environment_review"], "check_vs_runtime": [],
    "start_vs": [], "vs_status": [],
    "evaluate_vs": [], "report_vs": [],
    "prepare_vs_refinement": ["candidate_ids", "reason"], "read_lo_input": ["evidence_path", "evidence_locator", "evidence_offset"],
    "review_lo_environment": ["environment_review"], "check_lo_runtime": [],
    "start_lo": [], "lo_status": [], "evaluate_lo": [], "report_lo": [],
    "screening_report": [], "select_screening_candidates": ["selection_policy", "reason"], "read_screening_candidates": [],
    "plan_refinement": ["refinement_review", "max_points"], "recover_refinement": [],
    "release_refinement": ["reason"],
    "approve_refinement": [], "refinement_status": [], "cancel_refinement": [], "wake_refinement": [], "refinement_report": [],
    "resume_refinement_delivery": [],
    "select_md_candidate": ["compound_id", "input_sdf_index", "reason"], "release_md_candidate": ["reason"],
    "prepare_md_input": [],
    "attach_historical_md_source": ["source_artifact_id", "source_manifest_sha256", "reason"],
    "read_historical_md_source": [], "prepare_historical_md_candidate": [],
    **{"plan_md_" + name: fields for name, fields in md_execution.PLAN_FIELDS.items()},
    **{action: [] for name in md_execution.STAGES for action in ("start_md_" + name, "md_" + name + "_status")},
    "cancel_md_stage": [], "release_md_stage": ["reason"], "retry_md_stage": ["reason"],
}

RESEARCH_ACTIONS = tuple(action for action in FIELDS if action not in RECEPTOR_ACTIONS)
