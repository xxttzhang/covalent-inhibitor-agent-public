"""Library first tool contracts and thin adapters; no scientific execution logic is duplicated."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .annotations import READ_ONLY, EXECUTION
from covscreen.library_pilot import PilotInput, prepare_pilot
from covscreen.library_site_handoff import LibrarySitePlanInput, plan_from_sites
from covscreen.site_contracts import input_schema
from covscreen.tool_api import (
    library_first_chain_tool,
    library_first_intake_tool,
    library_first_resolve_target_tool,
    library_first_discover_structures_tool,
    library_first_acquire_structure_tool,
    library_first_prepare_receptor_tool,
    library_first_assess_receptor_context_tool,
    library_first_parameterize_adduct_tool,
    library_first_prepare_openmm_system_tool,
    library_first_run_openmm_short_qc_tool,
    library_first_equilibrate_openmm_system_tool,
    library_first_run_openmm_production_tool,
    library_first_audit_openmm_trajectory_tool,
    library_first_analyze_openmm_replicas_tool,
    library_first_build_md_report_tool,
    library_first_deliver_md_candidates_tool,
    library_first_select_md_candidate_tool,
    library_first_admit_md_candidate_tool,
    library_first_next_md_action_tool,
    next_library_action_tool,
    plan_library_cascade_tool,
)


TOOLS = [
    {"name": "covalent_library_first_prepare_pilot",
     "description": "Build a fresh bounded Library-first prescreen request from a frozen selection ID/hash and selected canonical_position. Uses the registered real virtual library and curated site chemistry policy, never old candidate results. Defaults: scan the full CSV and reservoir-sample 1000 rows with fixed seed for chemistry, up to 10 candidates, 2 CPU/8GB/20min. Writes immutable inputs and returns an exact plan; does not compute. Invoke after plan_from_sites reports missing registered requests. Then covalent_submit_job triggers separate native approval. Docking/scoring needs subsequent approval; no MD.",
     "inputSchema": input_schema(PilotInput), "annotations": {**READ_ONLY, "readOnlyHint": False}},
    {"name": "covalent_library_first_plan_from_sites",
     "description": "Use immediately after shared covalent_select_covalent_sites for Library-first. Accept the selection artifact ID and exact hash, never a guessed path. Revalidate selection/evaluation/prediction ancestry and resolve matching registered route requests internally. Missing route inputs return explicit BLOCKED prerequisites; a selection JSON is NOT a LibraryFirstRequest. Read-only planning, no campaign registration, computation, parameterization or MD. Execution needs a separate exact native approval.",
     "inputSchema": input_schema(LibrarySitePlanInput), "annotations": READ_ONLY},
    {
        "name":"covalent_library_first_next_md_action",
        "description":"Read-only Library-first MD continuation planner. It replays the exact current-stage task/artifact evidence, invokes the existing next tool in plan mode, and returns the next registered call without submitting computation. It enforces three production replicas, three trajectory audits and expert review after the fixed report.",
        "inputSchema":{"type":"object","properties":{
            "stage":{"type":"string","enum":["candidate_delivery","candidate_pose",
                "adduct_admission","parameterization","system_preparation","short_qc",
                "equilibration","production_replicas","trajectory_audits",
                "structural_analysis","md_report"]},
            "delivery_run_id":{"type":"string"},"delivery_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "compound_id":{"type":"string"},"input_sdf_index":{"type":"integer","minimum":0},
            "preparation_run_id":{"type":"string"},"prepared_receptor_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "receptor_context_artifact_id":{"type":"string"},"receptor_context_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "candidate_pose_artifact_id":{"type":"string"},"manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "adduct_admission_run_id":{"type":"string"},"adduct_input_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "parameterization_run_id":{"type":"string"},"parameterization_audit_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "system_preparation_run_id":{"type":"string"},"preparation_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "short_qc_run_id":{"type":"string"},"short_qc_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "integrator_seed":{"type":"integer","minimum":1,"maximum":2147483647},
            "barostat_seed":{"type":"integer","minimum":1,"maximum":2147483647},
            "equilibration_run_id":{"type":"string"},"equilibration_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "seed_base":{"type":"integer","minimum":1,"maximum":2147483635},
            "production_replicas":{"type":"array","minItems":3,"maxItems":3,"items":{"type":"object","properties":{
                "run_id":{"type":"string"},"manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"}},
                "required":["run_id","manifest_sha256"],"additionalProperties":False}},
            "trajectory_audits":{"type":"array","minItems":3,"maxItems":3,"items":{"type":"object","properties":{
                "run_id":{"type":"string"},"manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"}},
                "required":["run_id","manifest_sha256"],"additionalProperties":False}},
            "structural_analysis_run_id":{"type":"string"},"structural_analysis_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "md_report_run_id":{"type":"string"},"md_report_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"}},
            "required":["stage"],"additionalProperties":False},"annotations":READ_ONLY},
    {
        "name":"covalent_library_first_admit_md_candidate",
        "description":"Compile, submit or inspect registered Schrodinger-native extraction and graph QC for one candidate-pose artifact, followed by hash-bound covalent-adduct admission. It accepts only an artifact ID/hash, emits a parameterization-pending adduct input, and does not assign force-field parameters or claim MD readiness.",
        "inputSchema":{"type":"object","properties":{
            "action":{"type":"string","enum":["plan","start","status"],"default":"plan"},
            "candidate_pose_artifact_id":{"type":"string","pattern":"^candidate-pose-input-[a-f0-9]{32}$"},
            "manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "native_timeout_seconds":{"type":"integer","minimum":60,"maximum":3600,"default":1200},
            "resource_budget":{"type":"object","properties":{
                "max_runtime_minutes":{"type":"integer","minimum":1,"maximum":60},
                "gpu_count":{"type":"integer","enum":[0],"default":0},
                "cpu_count":{"type":"integer","minimum":1,"maximum":4,"default":4},
                "memory_gb":{"type":"number","exclusiveMinimum":0,"maximum":16,"default":16},
                "max_candidates":{"type":"integer","enum":[1],"default":1}},
                "required":["max_runtime_minutes"],"additionalProperties":False},
            "human_approved":{"type":"boolean","default":False},"approval_note":{"type":["string","null"]},
            "run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"}},
            "required":["action"],"additionalProperties":False},"annotations":EXECUTION},
    {
        "name":"covalent_library_first_select_md_candidate",
        "description":"Select one exact compound/index pair from a completed frozen MD-candidate delivery and bind it to a completed prepared receptor plus receptor-context artifact. It resolves and copies all files internally, returns a candidate-pose artifact ID/hash for native graph QC, and does not inspect a Maestro graph, parameterize or start MD.",
        "inputSchema":{"type":"object","properties":{
            "action":{"type":"string","enum":["plan","select"],"default":"plan"},
            "delivery_run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            "delivery_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "compound_id":{"type":"string","minLength":1,"maxLength":256},
            "input_sdf_index":{"type":"integer","minimum":0},
            "preparation_run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            "prepared_receptor_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "receptor_context_artifact_id":{"type":"string","pattern":"^receptor-context-[a-f0-9]{32}$"},
            "receptor_context_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"}},
            "required":["action","delivery_run_id","delivery_manifest_sha256","compound_id",
                "input_sdf_index","preparation_run_id","prepared_receptor_manifest_sha256",
                "receptor_context_artifact_id","receptor_context_manifest_sha256"],
            "additionalProperties":False},"annotations":EXECUTION},
    {
        "name":"covalent_library_first_deliver_md_candidates",
        "description":"Freeze the candidates selected by one completed Library-first Pareto run, together with their exact AF3 and registered CovDock structure assets, for subsequent MD selection. It accepts only the Pareto run ID and exact manifest hash, follows the registered CovDock parent internally, exposes no source paths, and does not select a candidate or claim MD readiness.",
        "inputSchema":{"type":"object","properties":{
            "action":{"type":"string","enum":["plan","start","status"],"default":"plan"},
            "pareto_run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            "pareto_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "resource_budget":{"type":"object","properties":{
                "max_runtime_minutes":{"type":"integer","minimum":1,"maximum":30},
                "gpu_count":{"type":"integer","enum":[0],"default":0},
                "cpu_count":{"type":"integer","minimum":1,"maximum":2,"default":2},
                "memory_gb":{"type":"number","exclusiveMinimum":0,"maximum":8,"default":8},
                "max_candidates":{"type":"integer","minimum":1,"maximum":100,"default":100}},
                "required":["max_runtime_minutes"],"additionalProperties":False},
            "human_approved":{"type":"boolean","default":False},"approval_note":{"type":["string","null"]},
            "run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"}},
            "required":["action"],"additionalProperties":False},"annotations":EXECUTION},
    {
        "name":"covalent_library_first_build_md_report",
        "description":"Compile, submit or inspect the versioned candidate-level covalent MD structural-triage report. It accepts only a completed three-replica structural-analysis run and exact manifest hash, applies fixed v1 technical and consensus criteria, and emits JSON plus Markdown. The recommendation requires expert cross-evidence review and does not claim affinity, activity, kinetics or clinical value.",
        "inputSchema":{"type":"object","properties":{
            "action":{"type":"string","enum":["plan","start","status"],"default":"plan"},
            "structural_analysis_run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            "structural_analysis_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "resource_budget":{"type":"object","properties":{
                "max_runtime_minutes":{"type":"integer","minimum":1,"maximum":30},
                "gpu_count":{"type":"integer","enum":[0],"default":0},
                "cpu_count":{"type":"integer","minimum":1,"maximum":2,"default":2},
                "memory_gb":{"type":"number","exclusiveMinimum":0,"maximum":4,"default":4},
                "max_candidates":{"type":"integer","enum":[1],"default":1}},
                "required":["max_runtime_minutes"],"additionalProperties":False},
            "human_approved":{"type":"boolean","default":False},"approval_note":{"type":["string","null"]},
            "run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"}},
            "required":["action"],"additionalProperties":False},"annotations":EXECUTION},
    {
        "name":"covalent_library_first_analyze_openmm_replicas",
        "description":"Compile, submit or inspect one descriptive analysis that must consume formal trajectory audits for replicas 1, 2 and 3 from the same equilibration parent. It reports aligned protein/pocket/ligand RMSD, ligand COM displacement, reference-contact retention and covalent distance. It does not infer affinity, kinetics, convergence or make the screening decision.",
        "inputSchema":{"type":"object","properties":{
            "action":{"type":"string","enum":["plan","start","status"],"default":"plan"},
            "trajectory_audits":{"type":"array","minItems":3,"maxItems":3,"items":{"type":"object","properties":{
                "run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
                "manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"}},
                "required":["run_id","manifest_sha256"],"additionalProperties":False}},
            "resource_budget":{"type":"object","properties":{
                "max_runtime_minutes":{"type":"integer","minimum":1,"maximum":240},
                "gpu_count":{"type":"integer","enum":[0],"default":0},
                "cpu_count":{"type":"integer","minimum":1,"maximum":8,"default":8},
                "memory_gb":{"type":"number","exclusiveMinimum":0,"maximum":32,"default":32},
                "max_candidates":{"type":"integer","enum":[1],"default":1}},
                "required":["max_runtime_minutes"],"additionalProperties":False},
            "human_approved":{"type":"boolean","default":False},"approval_note":{"type":["string","null"]},
            "run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"}},
            "required":["action"],"additionalProperties":False},"annotations":EXECUTION},
    {
        "name":"covalent_library_first_audit_openmm_trajectory",
        "description":"Compile, submit or inspect the immutable integrity audit for one completed production replica. It accepts only a production run ID and exact manifest hash, independently verifies DCD structure, atom/frame counts, frame timing, file hashes and the covalent-bond distance. Passing enables later structural analysis but makes no binding, affinity, kinetics or convergence claim.",
        "inputSchema":{"type":"object","properties":{
            "action":{"type":"string","enum":["plan","start","status"],"default":"plan"},
            "production_run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            "production_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "resource_budget":{"type":"object","properties":{
                "max_runtime_minutes":{"type":"integer","minimum":1,"maximum":120},
                "gpu_count":{"type":"integer","enum":[0],"default":0},
                "cpu_count":{"type":"integer","minimum":1,"maximum":4,"default":4},
                "memory_gb":{"type":"number","exclusiveMinimum":0,"maximum":16,"default":16},
                "max_candidates":{"type":"integer","enum":[1],"default":1}},
                "required":["max_runtime_minutes"],"additionalProperties":False},
            "human_approved":{"type":"boolean","default":False},"approval_note":{"type":["string","null"]},
            "run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"}},
            "required":["action"],"additionalProperties":False},"annotations":EXECUTION},
    {
        "name":"covalent_library_first_run_openmm_production",
        "description":"Compile, submit or inspect one admitted 10 ns unrestrained NPT production replica. It accepts only a completed 500 ps equilibration run and exact manifest hash, derives three distinct seeds from replica 1-3, and writes DCD/checkpoint/state evidence. Completion remains trajectory-audit pending and makes no binding, affinity or kinetics claim.",
        "inputSchema":{"type":"object","properties":{
            "action":{"type":"string","enum":["plan","start","status"],"default":"plan"},
            "equilibration_run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            "equilibration_manifest_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "replica":{"type":"integer","minimum":1,"maximum":3},
            "seed_base":{"type":"integer","minimum":1,"maximum":2147483638,"default":20261020},
            "resource_budget":{"type":"object","properties":{
                "max_runtime_minutes":{"type":"integer","minimum":1,"maximum":1440},
                "gpu_count":{"type":"integer","minimum":0,"maximum":1},
                "cpu_count":{"type":"integer","minimum":1,"maximum":8,"default":4},
                "memory_gb":{"type":"number","exclusiveMinimum":0,"maximum":32,"default":32},
                "max_candidates":{"type":"integer","enum":[1],"default":1}},
                "required":["max_runtime_minutes","gpu_count"],"additionalProperties":False},
            "human_approved":{"type":"boolean","default":False},"approval_note":{"type":["string","null"]},
            "run_id":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"}},
            "required":["action"],"additionalProperties":False},"annotations":EXECUTION},
    {
        "name": "covalent_library_first_equilibrate_openmm_system",
        "description": (
            "Compile, submit or inspect the registered 500 ps unrestrained NPT equilibration protocol for "
            "one completed short-QC job. Runtime platform and device properties come from deployment config, "
            "while the tool freezes distinct integrator/barostat seeds and exact parent hashes. Completion "
            "requires thermodynamic and covalent-geometry stability but does not claim equilibrium convergence "
            "or start production dynamics."
        ),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["plan", "start", "status"], "default": "plan"},
            "short_qc_run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            "short_qc_manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "integrator_seed": {"type": "integer", "minimum": 1, "maximum": 2147483647, "default": 20261015},
            "barostat_seed": {"type": "integer", "minimum": 1, "maximum": 2147483647, "default": 20261016},
            "resource_budget": {"type": "object", "properties": {
                "max_runtime_minutes": {"type": "integer", "minimum": 1, "maximum": 720},
                "gpu_count": {"type": "integer", "minimum": 0, "maximum": 1},
                "cpu_count": {"type": "integer", "minimum": 1, "maximum": 8, "default": 4},
                "memory_gb": {"type": "number", "exclusiveMinimum": 0, "maximum": 32, "default": 32},
                "max_candidates": {"type": "integer", "enum": [1], "default": 1}},
                "required": ["max_runtime_minutes", "gpu_count"], "additionalProperties": False},
            "human_approved": {"type": "boolean", "default": False},
            "approval_note": {"type": ["string", "null"]},
            "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"}},
            "required": ["action"], "additionalProperties": False},
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_library_first_run_openmm_short_qc",
        "description": (
            "Compile, submit or inspect a bounded unrestrained CPU integration-stability gate for one "
            "completed OpenMM system-preparation job. It replays the exact parent chain and minimized "
            "energy/forces, then checks finite dynamics, constraints and the Cys-S-C bond. Passing remains "
            "equilibration-pending and does not establish binding stability or MD readiness."
        ),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["plan", "start", "status"], "default": "plan"},
            "system_preparation_run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            "preparation_manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "steps": {"type": "integer", "minimum": 10, "maximum": 10000, "default": 1000},
            "report_interval": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 100},
            "velocity_seed": {"type": "integer", "minimum": 1, "maximum": 2147483647, "default": 20261014},
            "resource_budget": {"type": "object", "properties": {
                "max_runtime_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
                "gpu_count": {"type": "integer", "enum": [0], "default": 0},
                "cpu_count": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
                "memory_gb": {"type": "number", "exclusiveMinimum": 0, "maximum": 16, "default": 16},
                "max_candidates": {"type": "integer", "enum": [1], "default": 1}},
                "required": ["max_runtime_minutes"], "additionalProperties": False},
            "human_approved": {"type": "boolean", "default": False},
            "approval_note": {"type": ["string", "null"]},
            "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"}},
            "required": ["action"], "additionalProperties": False},
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_library_first_prepare_openmm_system",
        "description": (
            "Compile, submit or inspect CMS-to-OpenMM conversion, independent force-field auditing and "
            "bounded CPU minimization for one completed covalent-adduct parameterization job. It consumes "
            "the prior run ID and exact parameterization-audit hash; arbitrary CMS paths are not accepted. "
            "A successful job remains short-QC pending and does not establish equilibration or MD readiness."
        ),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["plan", "start", "status"], "default": "plan"},
            "parameterization_run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            "parameterization_audit_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "max_minimization_iterations": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
            "resource_budget": {"type": "object", "properties": {
                "max_runtime_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
                "gpu_count": {"type": "integer", "enum": [0], "default": 0},
                "cpu_count": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
                "memory_gb": {"type": "number", "exclusiveMinimum": 0, "maximum": 16, "default": 16},
                "max_candidates": {"type": "integer", "enum": [1], "default": 1}},
                "required": ["max_runtime_minutes"], "additionalProperties": False},
            "human_approved": {"type": "boolean", "default": False},
            "approval_note": {"type": ["string", "null"]},
            "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"}},
            "required": ["action"], "additionalProperties": False},
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_library_first_parameterize_adduct",
        "description": (
            "Compile, submit or inspect native OPLS_2005 parameter assignment for exactly one admitted "
            "admitted covalent adduct. Prefer a completed MD-adduct-admission run ID/hash; the legacy "
            "state artifact contract remains readable. The job revalidates the candidate/receptor/context/QC parent "
            "chain and the native Cys-S-C graph before solvation and parameter audits. Starting compute "
            "requires native user approval and a registered Schrodinger runtime. Successful parameters still "
            "require minimization, short QC, conversion and MD-system admission."
        ),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["plan", "start", "status"], "default": "plan"},
            "adduct_input_artifact_id": {"type": "string", "pattern": "^covalent-adduct-input-[a-f0-9]{32}$"},
            "manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "adduct_admission_run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            "adduct_input_manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "native_timeout_seconds": {"type": "integer", "minimum": 60, "maximum": 3600, "default": 1200},
            "resource_budget": {"type": "object", "properties": {
                "max_runtime_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
                "gpu_count": {"type": "integer", "enum": [0], "default": 0},
                "cpu_count": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
                "memory_gb": {"type": "number", "exclusiveMinimum": 0, "maximum": 16, "default": 8},
                "max_candidates": {"type": "integer", "enum": [1], "default": 1}},
                "required": ["max_runtime_minutes"], "additionalProperties": False},
            "human_approved": {"type": "boolean", "default": False},
            "approval_note": {"type": ["string", "null"]},
            "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"}},
            "required": ["action"], "additionalProperties": False},
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_library_first_assess_receptor_context",
        "description": (
            "Create a hash-bound descriptive context artifact for a completed prepared receptor. Measures "
            "target-Cys sulfur/residue SASA, the local protein shell, and nearby deposited ligands, waters "
            "and other chains. It revalidates both acquisition and preparation parents. Distances and SASA "
            "do not establish cysteine reactivity, ligandability, assembly suitability or campaign readiness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "receptor_input_artifact_id": {"type": "string", "pattern": "^receptor-input-[a-f0-9]{32}$"},
                "manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "preparation_run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
                "prepared_receptor_manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            },
            "required": ["receptor_input_artifact_id", "manifest_sha256", "preparation_run_id",
                         "prepared_receptor_manifest_sha256"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "covalent_library_first_prepare_receptor",
        "description": (
            "Compile, submit, or inspect the registered CPU receptor-preparation pipeline for a verified "
            "receptor-input artifact. The current and only repair backend is bundled PDBFixer 1.12.0 followed "
            "by a registered OpenMM runtime; Schrodinger Prime is not implemented and the two backends must "
            "not be chained in one receptor lineage. It revalidates parent evidence, repairs missing protein atoms, relaxes "
            "added atoms, independently audits identity, stereochemistry and geometry, and admits only an "
            "isolated selected-chain protein scope. Starting compute requires native user approval. A passing "
            "result does not establish biological assembly, pocket suitability, cysteine reactivity or campaign readiness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["plan", "start", "status"], "default": "plan"},
                "receptor_input_artifact_id": {"type": "string", "pattern": "^receptor-input-[a-f0-9]{32}$"},
                "manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "altlocs": {"type": "object", "additionalProperties": {"type": "string", "pattern": "^[A-Za-z0-9]$"}, "default": {}},
                "heavy_seed": {"type": "integer", "minimum": 1, "maximum": 2147483647, "default": 20261013},
                "max_missing_residues": {"type": "integer", "minimum": 0, "maximum": 100, "default": 30},
                "resource_budget": {
                    "type": "object", "properties": {
                        "max_runtime_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
                        "gpu_count": {"type": "integer", "enum": [0], "default": 0},
                        "cpu_count": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
                        "memory_gb": {"type": "number", "exclusiveMinimum": 0, "maximum": 16, "default": 4},
                        "max_candidates": {"type": "integer", "enum": [1], "default": 1},
                    }, "required": ["max_runtime_minutes"], "additionalProperties": False,
                },
                "human_approved": {"type": "boolean", "default": False},
                "approval_note": {"type": ["string", "null"]},
                "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$"},
            },
            "required": ["action"], "additionalProperties": False,
        },
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_library_first_acquire_structure",
        "description": (
            "Acquire one entry from a verified structure-discovery artifact and map an explicit canonical Cys "
            "to author numbering. Revalidates both parent artifacts. Audits complete site heavy atoms, model, "
            "alternate locations, occupancy and explicit sulfur connections without requiring a ligand. "
            "Plan is offline; acquire downloads bounded public RCSB evidence. Does not repair the receptor, "
            "validate site reactivity, parameterize an adduct or launch compute."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "discovery_artifact_id": {"type": "string", "pattern": "^structures-[a-f0-9]{32}$"},
                "discovery_manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "entry": {"type": "string", "pattern": "^[0-9][A-Z0-9]{3}$"},
                "cys": {"type": "integer", "minimum": 1},
                "label_chain": {"type": ["string", "null"]},
                "model": {"type": ["integer", "null"], "minimum": 1},
                "altloc": {"type": ["string", "null"]},
                "action": {"type": "string", "enum": ["plan", "acquire"], "default": "plan"},
            },
            "required": ["discovery_artifact_id", "discovery_manifest_sha256", "entry", "cys"],
            "additionalProperties": False,
        },
        "annotations": {**READ_ONLY, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "covalent_library_first_discover_structures",
        "description": (
            "Find candidate PDB entries from a verified Library-first target artifact, without a parent ligand. "
            "Requires the target artifact ID and manifest SHA256 returned by resolve_target; rechecks raw "
            "UniProt and FASTA evidence before use. Plan is offline; search makes one bounded RCSB query. "
            "Entry hits do not verify chain, mutation, site mapping, pocket quality or receptor readiness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_artifact_id": {"type": "string", "pattern": "^target-[a-f0-9]{32}$"},
                "target_manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "action": {"type": "string", "enum": ["plan", "search"], "default": "plan"},
            },
            "required": ["target_artifact_id", "target_manifest_sha256"],
            "additionalProperties": False,
        },
        "annotations": {**READ_ONLY, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "covalent_library_first_resolve_target",
        "description": (
            "Resolve an explicit primary gene symbol and species to reviewed UniProt identity and canonical "
            "sequence, without requiring a ligand or a preselected Cys. Plan is offline; search makes one bounded "
            "public lookup and saves hash-bound evidence. Canonical cysteine positions are not validated reactive "
            "sites. Ambiguous or incomplete lookup never selects a target. Does not prepare a receptor or submit compute."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "gene": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$"},
                "taxon": {"type": "integer", "minimum": 1},
                "action": {"type": "string", "enum": ["plan", "search"], "default": "plan"},
            },
            "required": ["gene", "taxon"],
            "additionalProperties": False,
        },
        "annotations": {**READ_ONLY, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "covalent_library_first_intake",
        "description": (
            "Resolve a natural-language request such as '针对 EGFR C797 从 CovalentInDB 筛选共价抑制剂' "
            "against the registered target/site and executable Campaign. Returns READY or an explicit "
            "site, scope, campaign, or input blocker; never launches computation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "source_library": {"type": "string", "default": "CovalentInDB 2.0"},
                "scope": {
                    "type": "string",
                    "enum": ["auto", "pilot", "full"],
                    "default": "auto",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_library_first_chain",
        "description": (
            "Inspect the formal parent-linked CovalentInDB Library-first campaign, enforce the "
            "completed stage quality gate, compile its sole legal next registered request, and "
            "optionally submit it. Execute submission requires explicit approval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "config_path": {"type": "string"},
                "execution_mode": {
                    "type": "string",
                    "enum": ["plan", "execute"],
                    "default": "plan",
                },
                "submit": {"type": "boolean", "default": False},
                "human_approved": {"type": "boolean", "default": False},
                "approval_note": {"type": ["string", "null"]},
            },
            "required": ["config_path"],
            "additionalProperties": False,
        },
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_plan_library_cascade",
        "description": (
            "Validate a Library-first request and build a cost- and uncertainty-adaptive "
            "CarsiDock/RTMScore/AF3/CovDock cascade. request_path must point to a registered LibraryFirstRequest, NOT a site-selection artifact. After shared site selection use covalent_library_first_plan_from_sites with artifact ID/hash instead. Never launches an engine."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_path": {"type": "string"},
                "library_size": {"type": ["integer", "null"], "minimum": 1},
                "route_override_approved": {"type": "boolean", "default": False},
                "allow_proprietary": {"type": "boolean", "default": False},
                "require_inputs": {"type": "boolean", "default": True},
                "availability": {"type": ["object", "null"]},
                "policy": {"type": ["object", "null"]},
            },
            "required": ["request_path"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_next_library_action",
        "description": (
            "Choose the next cascade action from explicit stage observations, failures, "
            "uncertainty, and remaining GPU budget."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan": {"type": "object"},
                "observation": {"type": "object"},
                "policy": {"type": ["object", "null"]},
            },
            "required": ["plan", "observation"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
]


def handlers(root: Path, arguments: dict[str, Any]) -> dict[str, Callable[[], dict[str, Any]]]:
    return {
        'covalent_library_first_prepare_pilot': lambda: prepare_pilot(root, arguments),
        'covalent_library_first_plan_from_sites': lambda: plan_from_sites(root, arguments),
        'covalent_library_first_next_md_action':lambda:library_first_next_md_action_tool(root,arguments),
        'covalent_library_first_admit_md_candidate':lambda:library_first_admit_md_candidate_tool(root,arguments),
        'covalent_library_first_select_md_candidate':lambda:library_first_select_md_candidate_tool(root,arguments),
        'covalent_library_first_deliver_md_candidates':lambda:library_first_deliver_md_candidates_tool(root,arguments),
        'covalent_library_first_build_md_report':lambda:library_first_build_md_report_tool(root,arguments),
        'covalent_library_first_analyze_openmm_replicas':lambda:library_first_analyze_openmm_replicas_tool(root,arguments),
        'covalent_library_first_audit_openmm_trajectory':lambda:library_first_audit_openmm_trajectory_tool(root,arguments),
        'covalent_library_first_run_openmm_production':lambda:library_first_run_openmm_production_tool(root,arguments),
        'covalent_library_first_equilibrate_openmm_system': lambda: library_first_equilibrate_openmm_system_tool(root, arguments),
        'covalent_library_first_run_openmm_short_qc': lambda: library_first_run_openmm_short_qc_tool(root, arguments),
        'covalent_library_first_prepare_openmm_system': lambda: library_first_prepare_openmm_system_tool(root, arguments),
        'covalent_library_first_parameterize_adduct': lambda: library_first_parameterize_adduct_tool(root, arguments),
        'covalent_library_first_assess_receptor_context': lambda: library_first_assess_receptor_context_tool(root, arguments),
        'covalent_library_first_prepare_receptor': lambda: library_first_prepare_receptor_tool(root, arguments),
        'covalent_library_first_acquire_structure': lambda: library_first_acquire_structure_tool(
            root, arguments["discovery_artifact_id"], arguments["discovery_manifest_sha256"],
            arguments["entry"], arguments["cys"], label_chain=arguments.get("label_chain"),
            model=arguments.get("model"), altloc=arguments.get("altloc"), action=arguments.get("action", "plan")
        ),
        'covalent_library_first_discover_structures': lambda: library_first_discover_structures_tool(
            root, arguments["target_artifact_id"], arguments["target_manifest_sha256"],
            limit=arguments.get("limit", 20), action=arguments.get("action", "plan")
        ),
        'covalent_library_first_resolve_target': lambda: library_first_resolve_target_tool(
            root, arguments["gene"], arguments["taxon"], action=arguments.get("action", "plan")
        ),
        'covalent_library_first_intake': lambda: library_first_intake_tool(
            root,
            arguments["query"],
            source_library=arguments.get("source_library", "CovalentInDB 2.0"),
            scope=arguments.get("scope", "auto"),
        ),
        'covalent_library_first_chain': lambda: library_first_chain_tool(
            root,
            arguments["config_path"],
            execution_mode=arguments.get("execution_mode", "plan"),
            submit=arguments.get("submit", False),
            human_approved=arguments.get("human_approved", False),
            approval_note=arguments.get("approval_note"),
        ),
        'covalent_plan_library_cascade': lambda: plan_library_cascade_tool(
            root,
            arguments["request_path"],
            library_size=arguments.get("library_size"),
            route_override_approved=arguments.get("route_override_approved", False),
            allow_proprietary=arguments.get("allow_proprietary", False),
            require_inputs=arguments.get("require_inputs", True),
            availability=arguments.get("availability"),
            policy=arguments.get("policy"),
        ),
        'covalent_next_library_action': lambda: next_library_action_tool(
            arguments["plan"], arguments["observation"], arguments.get("policy")
        ),
    }
