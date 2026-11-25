from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from covscreen.tool_api import (
    cancel_job_tool,
    capabilities_tool,
    enumerate_covalent_candidates_tool,
    get_job_candidates_tool,
    get_job_log_tool,
    get_job_tool,
    list_jobs_tool,
    next_library_action_tool,
    platform_health_tool,
    plan_attachments_tool,
    plan_ligand_first_tool,
    plan_de_novo_tool,
    plan_library_cascade_tool,
    runtime_preflight,
    retry_job_tool,
    submit_job_tool,
    validate_project_tool,
)
from covscreen.jobs import ArtifactRecord, JobSpec, JobSubmissionRequest, ResourceBudget, RunResult
from covscreen.attachment import AttachmentPlanResult, AttachmentRequest
from covscreen.covalent_enumerator import (
    CovalentEnumerationRequest,
    CovalentEnumerationResult,
)
from covscreen.cascade import (
    AdaptiveCascadePlan,
    CascadeNextAction,
    CascadeObservation,
    CascadePolicy,
    EngineAvailability,
)
from covscreen.ligand_first import (
    CovalentGeometryPolicy,
    LigandFirstGenerationPolicy,
    LigandFirstPlan,
    LigandFirstRequest,
    LigandFirstStage,
)
from covscreen.de_novo import (
    DeNovoGenerationPolicy,
    DeNovoGeneratorAvailability,
    DeNovoPlan,
    DeNovoRequest,
    DeNovoStage,
    MultiObjectivePolicy,
    PocketDefinition,
)
from covscreen.research import write_json


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description="Covalent discovery planning and approved job tools.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities")
    commands.add_parser("runtime-preflight")
    commands.add_parser("platform-health")
    submit_job = commands.add_parser("submit-job")
    submit_job.add_argument("--request", type=Path, required=True)
    list_jobs = commands.add_parser("list-jobs")
    list_jobs.add_argument("--limit", type=int, default=50)
    get_job = commands.add_parser("get-job")
    get_job.add_argument("--run-id", required=True)
    get_log = commands.add_parser("get-job-log")
    get_log.add_argument("--run-id", required=True)
    get_log.add_argument("--stream", choices=["stdout", "stderr"], default="stdout")
    get_log.add_argument("--max-lines", type=int, default=200)
    get_candidates = commands.add_parser("get-candidates")
    get_candidates.add_argument("--run-id", required=True)
    get_candidates.add_argument("--limit", type=int, default=200)
    cancel_job = commands.add_parser("cancel-job")
    cancel_job.add_argument("--run-id", required=True)
    cancel_job.add_argument("--human-approved", action="store_true")
    retry_job = commands.add_parser("retry-job")
    retry_job.add_argument("--run-id", required=True)
    retry_job.add_argument("--execution-mode", choices=["plan", "execute"], default="execute")
    retry_job.add_argument("--human-approved", action="store_true")
    retry_job.add_argument("--approval-note", required=True)
    schemas = commands.add_parser("write-schemas")
    schemas.add_argument("--schema-dir", type=Path, default=ROOT / "schemas")
    validate = commands.add_parser("validate-project")
    validate.add_argument("--project-id", required=True)
    cascade = commands.add_parser("plan-library-cascade")
    cascade.add_argument("--request", required=True)
    cascade.add_argument("--library-size", type=int)
    cascade.add_argument("--route-override-approved", action="store_true")
    cascade.add_argument("--allow-proprietary", action="store_true")
    cascade.add_argument("--allow-missing-inputs", action="store_true")
    next_action = commands.add_parser("next-library-action")
    next_action.add_argument("--plan", type=Path, required=True)
    next_action.add_argument("--observation", type=Path, required=True)
    attachment = commands.add_parser("plan-attachments")
    attachment.add_argument("--request", type=Path, required=True)
    enumerate_warheads = commands.add_parser("enumerate-warheads")
    enumerate_warheads.add_argument("--request", type=Path, required=True)
    ligand_first = commands.add_parser("plan-ligand-first")
    ligand_first.add_argument("--request", required=True)
    ligand_first.add_argument("--allow-missing-inputs", action="store_true")
    de_novo = commands.add_parser("plan-de-novo")
    de_novo.add_argument("--request", required=True)
    de_novo.add_argument("--allow-missing-inputs", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "capabilities":
            result = capabilities_tool(ROOT)
        elif args.command == "runtime-preflight":
            result = runtime_preflight(ROOT)
        elif args.command == "platform-health":
            result = platform_health_tool(ROOT)
        elif args.command == "submit-job":
            result = submit_job_tool(ROOT, load(args.request))
        elif args.command == "list-jobs":
            result = list_jobs_tool(ROOT, limit=args.limit)
        elif args.command == "get-job":
            result = get_job_tool(ROOT, args.run_id)
        elif args.command == "get-job-log":
            result = get_job_log_tool(
                ROOT, args.run_id, stream=args.stream, max_lines=args.max_lines
            )
        elif args.command == "get-candidates":
            result = get_job_candidates_tool(ROOT, args.run_id, limit=args.limit)
        elif args.command == "cancel-job":
            result = cancel_job_tool(
                ROOT, args.run_id, human_approved=args.human_approved
            )
        elif args.command == "retry-job":
            result = retry_job_tool(
                ROOT,
                args.run_id,
                human_approved=args.human_approved,
                approval_note=args.approval_note,
                execution_mode=args.execution_mode,
            )
        elif args.command == "write-schemas":
            models = {
                "engine-availability": EngineAvailability,
                "cascade-policy": CascadePolicy,
                "adaptive-cascade-plan": AdaptiveCascadePlan,
                "cascade-observation": CascadeObservation,
                "cascade-next-action": CascadeNextAction,
                "attachment-request": AttachmentRequest,
                "attachment-plan": AttachmentPlanResult,
                "covalent-enumeration-request": CovalentEnumerationRequest,
                "covalent-enumeration-result": CovalentEnumerationResult,
                "ligand-first-generation-policy": LigandFirstGenerationPolicy,
                "covalent-geometry-policy": CovalentGeometryPolicy,
                "ligand-first-request": LigandFirstRequest,
                "ligand-first-stage": LigandFirstStage,
                "ligand-first-plan": LigandFirstPlan,
                "pocket-definition": PocketDefinition,
                "de-novo-generator-availability": DeNovoGeneratorAvailability,
                "de-novo-generation-policy": DeNovoGenerationPolicy,
                "multi-objective-policy": MultiObjectivePolicy,
                "de-novo-request": DeNovoRequest,
                "de-novo-stage": DeNovoStage,
                "de-novo-plan": DeNovoPlan,
                "resource-budget": ResourceBudget,
                "job-submission-request": JobSubmissionRequest,
                "job-spec": JobSpec,
                "run-result": RunResult,
                "artifact-record": ArtifactRecord,
            }
            outputs = []
            for name, model in models.items():
                path = args.schema_dir / f"{name}.schema.json"
                write_json(path, model.model_json_schema())
                outputs.append(str(path))
            result = {"status": "PASS", "schemas": outputs}
        elif args.command == "validate-project":
            result = validate_project_tool(ROOT, args.project_id)
        elif args.command == "plan-library-cascade":
            result = plan_library_cascade_tool(
                ROOT,
                args.request,
                library_size=args.library_size,
                route_override_approved=args.route_override_approved,
                allow_proprietary=args.allow_proprietary,
                require_inputs=not args.allow_missing_inputs,
            )
        elif args.command == "next-library-action":
            result = next_library_action_tool(load(args.plan), load(args.observation))
        elif args.command == "plan-ligand-first":
            result = plan_ligand_first_tool(
                ROOT,
                args.request,
                require_inputs=not args.allow_missing_inputs,
            )
        elif args.command == "plan-de-novo":
            result = plan_de_novo_tool(
                ROOT,
                args.request,
                require_inputs=not args.allow_missing_inputs,
            )
        elif args.command == "enumerate-warheads":
            result = enumerate_covalent_candidates_tool(load(args.request))
        else:
            result = plan_attachments_tool(ROOT, load(args.request))
    except Exception as exc:
        result = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
