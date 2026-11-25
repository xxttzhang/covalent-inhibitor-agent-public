"""Migration ownership for the public tool contract.

This catalog is descriptive, not an authorization rule or an active route
allowlist. Compatibility discovery remains intact until each route's backend
and host projection have been migrated and verified together.
"""
from __future__ import annotations

import hashlib
import json

CATALOG_VERSION = "route-tool-catalog-v1"

# Every callable has exactly one disposition. New tools must be classified
# explicitly; an unknown tool must not silently inherit public visibility.
DISPOSITIONS = {
    "research_entry": (
        "covalent_ligand_workflow",
        "covalent_library_workflow",
    ),
    "stage_tool": (
        "covalent_ligand_evidence", "covalent_ligand_design_step",
        "covalent_library_research", "covalent_library_receptor",
        "covalent_target_hypothesis", "covalent_capabilities",
        "covalent_validate_project", "covalent_runtime_preflight",
        "covalent_platform_health", "covalent_verify_report_claims",
        "covalent_library_first_prepare_docking",
        "covalent_predict_cysteine_sites", "covalent_evaluate_covalent_sites",
        "covalent_select_covalent_sites",
        "covalent_library_first_prepare_pilot", "covalent_library_first_plan_from_sites",
        "covalent_library_first_admit_md_candidate", "covalent_library_first_select_md_candidate",
        "covalent_library_first_deliver_md_candidates", "covalent_library_first_build_md_report",
        "covalent_library_first_analyze_openmm_replicas", "covalent_library_first_audit_openmm_trajectory",
        "covalent_library_first_run_openmm_production", "covalent_library_first_equilibrate_openmm_system",
        "covalent_library_first_run_openmm_short_qc", "covalent_library_first_prepare_openmm_system",
        "covalent_library_first_parameterize_adduct", "covalent_library_first_assess_receptor_context",
        "covalent_library_first_prepare_receptor", "covalent_library_first_acquire_structure",
        "covalent_library_first_discover_structures", "covalent_library_first_resolve_target",
        "covalent_ligand_first_resolve_names", "covalent_ligand_first_prepare_inputs",
        "covalent_ligand_first_discover_structures", "covalent_ligand_first_audit_structure_site",
        "covalent_plan_attachments", "covalent_enumerate_warheads",
        "covalent_evaluate_ligand_first_candidate", "covalent_get_candidates",
        "covalent_plan_de_novo", "covalent_de_novo_review",
    ),
    "internal_execution": (
        "covalent_prepare_execution", "covalent_submit_job",
        "covalent_lookup_submission", "covalent_wait_job", "covalent_verify_job_result",
        "covalent_get_guard_status", "covalent_list_workflows", "covalent_resume_workflow",
        "covalent_list_jobs", "covalent_get_job", "covalent_get_job_log",
        "covalent_cancel_job", "covalent_retry_job",
        "covalent_library_first_next_md_action", "covalent_next_library_action",
    ),
    "compatibility": (
        "covalent_ligand_design", "covalent_library_first_intake",
        "covalent_library_first_chain", "covalent_plan_library_cascade",
        "covalent_ligand_first_intake", "covalent_ligand_first_chain",
        "covalent_ligand_first_campaign", "covalent_plan_ligand_first",
        "covalent_ligand_first_reuse_result",
    ),
}

PURPOSE = {
    "research_entry": "Start, inspect or resume the route's durable scientific workflow",
    "stage_tool": "Expose through the route's current-stage adapter with bound scientific assets",
    "internal_execution": "Workflow/host operation; retain raw protocol until consumers migrate",
    "compatibility": "Retain existing implementation and callers; retire default discovery only after verified replacement",
}


def catalog(definitions, groups):
    """Join current schemas and ownership, rejecting drift rather than guessing."""
    policies = {}
    for disposition, names in DISPOSITIONS.items():
        for name in names:
            if name in policies:
                raise ValueError(f"Duplicate tool disposition: {name}")
            policies[name] = disposition
    names = [tool["name"] for tool in definitions]
    if len(names) != len(set(names)) or set(names) != set(policies):
        raise ValueError(f"Tool catalog drift: unclassified={sorted(set(names)-set(policies))}; "
                         f"removed={sorted(set(policies)-set(names))}")
    ownership = {}
    for group in groups:
        for name in group["tools"]:
            if name in ownership:
                raise ValueError(f"Duplicate tool owner: {name}")
            ownership[name] = group
    if set(ownership) != set(names):
        raise ValueError("Tool ownership differs from registered tools")
    schemas = {tool["name"]: tool.get("inputSchema", {}) for tool in definitions}
    encoded = {name: json.dumps(schema, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
               for name, schema in schemas.items()}
    return {"schema": CATALOG_VERSION, "visibility_enforced": False,
        "scientific_readiness_verified": False,
        "measurement_scope": "Unprojected local registry input schemas; not live model visibility or token counts",
        "input_schema_bytes": sum(len(value) for value in encoded.values()),
        "workflow_interfaces": [schema["x-research-workflow"] for schema in schemas.values()
                                if "x-research-workflow" in schema],
        "scope": "Migration catalog; current stage and authorization must be checked by the backend",
        "tools": [{"name": name, "disposition": policies[name],
                   "purpose": PURPOSE[policies[name]],
                   "owner": ownership[name]["group"], "module": ownership[name]["module"],
                   "routes": ownership[name]["routes"],
                   "input_schema_bytes": len(encoded[name]),
                   "input_schema_sha256": hashlib.sha256(encoded[name]).hexdigest(),
                   "input_fields": sorted(schemas[name].get("properties", {})),
                   "declared_actions": schemas[name].get("properties", {}).get("action", {}).get("enum", []),
                   "raw_protocol_retained": True} for name in names]}
