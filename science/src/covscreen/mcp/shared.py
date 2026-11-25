"""Shared tool contracts and thin adapters; no scientific execution logic is duplicated."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from covscreen.pilot_docking import PilotDockingInput, prepare_docking
from covscreen.result_verification import verify_job
from covscreen.workflow_state import list_workflows, resume_workflow
from covscreen.submission_identity import SubmissionLookupInput, lookup_submission
from covscreen.branch_guard import GuardStatusInput, guard_status
from covscreen.report_claims import ReportClaimsInput, verify_report_claims

from .annotations import READ_ONLY, EXECUTION, CANCELLATION
from covscreen.tool_api import (
    cancel_job_tool,
    capabilities_tool,
    get_job_candidates_tool,
    get_job_log_tool,
    get_job_tool,
    wait_job_tool,
    list_jobs_tool,
    platform_health_tool,
    retry_job_tool,
    runtime_preflight,
    validate_project_tool,
)


TOOLS = [
    {"name": "covalent_verify_report_claims",
     "description": "Check structured report prerequisites against the live tool registry and persisted study stop. Detect incorrect tool counts and unsupported claims about host approval availability or total resource readiness. For current branch/stop claims, supply execution_stop_scope, quarantined_job_count and guard_snapshot_sha256 from a refreshed guard observation; pre-failure snapshots cannot establish current state. When describing stop-gate access, also supply stop_gate_tool_count, stop_gate_all_tools_read_only and cancellation_requires_native_approval; the stop-gate list is not an execution permission or a read-only list. CORRECTION_REQUIRED requests report correction, not computation retry. PASS covers only explicitly supplied named fields, not free prose or scientific results. Never approves, submits or clears stops.",
     "inputSchema": ReportClaimsInput.model_json_schema(), "annotations": READ_ONLY},
    {"name": "covalent_get_guard_status",
     "description": "Discover persistent study stops and quarantined job roots after reconnecting. Reports the original failure category and affected root jobs; downstream dependency enforcement remains active. Read-only, never clears a stop or grants execution approval. PASS means inspection succeeded, not that execution is allowed.",
     "inputSchema": GuardStatusInput.model_json_schema(), "annotations": READ_ONLY},
    {"name": "covalent_lookup_submission",
     "description": "Read the persistent receipt for an exact project/request/execution-mode identity after a lost submission response. Returns the bound existing run_id when registered. Never submits, clears uncertainty, restores approval, or verifies scientific outputs. Missing evidence never authorizes resubmission.",
     "inputSchema": SubmissionLookupInput.model_json_schema(), "annotations": READ_ONLY},
    {"name": "covalent_library_first_prepare_docking",
     "description": "Continue a verified site-bound pilot using its prescreen run ID. Validates frozen site identity, registered experimental receptor/reference coordinates and candidate lineage; prepares a route through docking and RTMScore and the next exact native-approval request. Does not launch calculations. Missing registered assets are reported, never invented.",
     "inputSchema": PilotDockingInput.model_json_schema(),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}},
    {
        "name": "covalent_list_workflows",
        "description": "Discover persisted workflows after reconnecting. Listed states are historical observations, not current execution authority; use resume_workflow to revalidate.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}}, "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_resume_workflow",
        "description": "Revalidate a registered workflow and generate its next plan without submitting. Checks configuration identity and live parent results; never restores approval from a snapshot.",
        "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}, "required": ["workflow_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "covalent_capabilities",
        "description": "List registered capabilities, producer-scoped metric definitions and interpretation limits, frozen-policy rules, and the execution boundary. Metric contracts describe measurements; they do not validate thresholds or establish biological activity.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_validate_project",
        "description": "Validate a project dossier and report target, site, evidence decision, and route.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_runtime_preflight",
        "description": (
            "Inspect Python, CPU, CUDA and engine availability without launching scientific calculations. "
            "Status reports whether inspection succeeded; callers must compare the next tool's exact "
            "resource budget with resource_classes. Missing CUDA never blocks a CPU-only tool."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_platform_health",
        "description": (
            "Check the active OpenScience configuration, MCP job registry, disk, RDKit and registered "
            "engine assets without launching calculations. Judge only failures relevant to the exact next tool."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_list_jobs",
        "description": "List persisted jobs and their current status for the OpenScience task view.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}},
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_get_job",
        "description": "Return a persisted JobSpec, RunResult, artifact list and provenance for one run.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_verify_job_result",
        "description": "Verify completed job identity, frozen inputs, registered output hashes and engine quality gates. PASS/verified=true is required before advancing; unsupported engines fail closed. This does not start calculations or approve the next stage.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"], "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_wait_job",
        "description": "Wait at most 30 seconds for an existing job. QUEUED/RUNNING after observation timeout is normal: wait again on the same run_id, never resubmit or use Python sleep. COMPLETED requires output verification before advancing. Failed jobs require reporting and separate approval for retry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 30, "default": 20},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_get_job_log",
        "description": "Return the tail of an approved job stdout or stderr log.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "stream": {"type": "string", "enum": ["stdout", "stderr"], "default": "stdout"},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_get_candidates",
        "description": "Return a bounded candidate or score table from one completed job for display in OpenScience.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_cancel_job",
        "description": "Request cancellation of a supported queued or running job. Explicit human approval is required. Acknowledgement is not terminal: observe the same run_id until the worker confirms termination. Unsupported external scheduler backends fail without claiming cancellation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "human_approved": {"type": "boolean", "const": True},
            },
            "required": ["run_id", "human_approved"],
            "additionalProperties": False,
        },
        "annotations": CANCELLATION,
    },
    {
        "name": "covalent_retry_job",
        "description": "Retry a failed, timed-out, or cancelled registered job as a new traceable attempt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "execution_mode": {"type": "string", "enum": ["plan", "execute"], "default": "execute"},
                "human_approved": {"type": "boolean", "const": True},
                "approval_note": {"type": "string", "minLength": 8},
            },
            "required": ["run_id", "human_approved", "approval_note"],
            "additionalProperties": False,
        },
        "annotations": EXECUTION,
    },
]


def handlers(root: Path, arguments: dict[str, Any]) -> dict[str, Callable[[], dict[str, Any]]]:
    return {
        'covalent_verify_report_claims': lambda: verify_report_claims(root, arguments),
        'covalent_get_guard_status': lambda: guard_status(root, arguments),
        'covalent_lookup_submission': lambda: lookup_submission(root, arguments),
        'covalent_list_workflows': lambda: list_workflows(root, arguments.get('limit', 50)),
        'covalent_resume_workflow': lambda: resume_workflow(root, arguments['workflow_id']),
        'covalent_library_first_prepare_docking': lambda: prepare_docking(root, arguments),
        'covalent_capabilities': lambda: capabilities_tool(root),
        'covalent_validate_project': lambda: validate_project_tool(root, arguments["project_id"]),
        'covalent_runtime_preflight': lambda: runtime_preflight(root),
        'covalent_platform_health': lambda: platform_health_tool(root),
        'covalent_list_jobs': lambda: list_jobs_tool(root, limit=arguments.get("limit", 50)),
        'covalent_verify_job_result': lambda: verify_job(root, arguments['run_id']),
        'covalent_wait_job': lambda: wait_job_tool(root, arguments['run_id'], timeout_seconds=arguments.get('timeout_seconds', 20)),
        'covalent_get_job': lambda: get_job_tool(root, arguments["run_id"]),
        'covalent_get_job_log': lambda: get_job_log_tool(
            root,
            arguments["run_id"],
            stream=arguments.get("stream", "stdout"),
            max_lines=arguments.get("max_lines", 200),
        ),
        'covalent_get_candidates': lambda: get_job_candidates_tool(
            root, arguments["run_id"], limit=arguments.get("limit", 200)
        ),
        'covalent_cancel_job': lambda: cancel_job_tool(
            root, arguments["run_id"], human_approved=arguments.get("human_approved", False)
        ),
        'covalent_retry_job': lambda: retry_job_tool(
            root,
            arguments["run_id"],
            human_approved=arguments.get("human_approved", False),
            approval_note=arguments.get("approval_note", ""),
            execution_mode=arguments.get("execution_mode", "execute"),
        ),
    }
