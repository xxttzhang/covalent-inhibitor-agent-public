from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pandas as pd

from covscreen.pareto import select_pareto_candidates
from covscreen.jobs import ArtifactRegistry, sha256_file, utc_now


def _runtime_and_resources(
    registry: ArtifactRegistry,
    run_id: str,
    result: object,
) -> tuple[float | None, dict[str, object] | None, dict[str, float] | None]:
    runtime = getattr(result, "runtime_seconds", None)
    runtime = float(runtime) if isinstance(runtime, (int, float)) and runtime >= 0 else None
    try:
        spec = registry.load_spec(run_id)
    except (AttributeError, FileNotFoundError):
        spec = None
    budget = getattr(spec, "resource_budget", None) if spec is not None else None
    if budget is None:
        return runtime, None, None
    resources = (
        budget.model_dump(mode="json")
        if hasattr(budget, "model_dump")
        else dict(budget) if isinstance(budget, dict) else None
    )
    if resources is None or runtime is None:
        return runtime, resources, None
    hours = runtime / 3600.0
    consumption = {
        "cpu_hours": hours * float(resources.get("cpu_count", 0) or 0),
        "gpu_hours": hours * float(resources.get("gpu_count", 0) or 0),
        "memory_gb_hours": hours * float(resources.get("memory_gb", 0) or 0),
    }
    return runtime, resources, consumption


def _retry_attempts(
    registry: ArtifactRegistry,
    run_id: str,
    result: object,
    lineage_edge: str | None,
) -> list[dict[str, object]]:
    """Return bounded retry ancestry without treating scientific parents as retries."""
    attempts: list[dict[str, object]] = []
    current_id, current = run_id, result
    seen: set[str] = set()
    while current_id not in seen:
        seen.add(current_id)
        runtime, resources, consumption = _runtime_and_resources(
            registry, current_id, current
        )
        attempts.append(
            {
                "run_id": current_id,
                "state": current.state,
                "return_code": current.return_code,
                "runtime_seconds": runtime,
                "requested_resources": resources,
                "resource_consumption": consumption,
            }
        )
        # parent_run_id is retry ancestry only when the scientific predecessor
        # is separately and explicitly recorded in the immutable spec.
        parent = current.parent_run_id if lineage_edge == "spec.parameters.upstream_run_id" else None
        if not parent:
            break
        previous = registry.load_result(parent)
        if (
            previous.route != current.route
            or previous.project_id != current.project_id
            or previous.engine != current.engine
        ):
            break
        current_id, current = parent, previous
    attempts.reverse()
    return attempts


def _scientific_upstream(
    registry: ArtifactRegistry,
    run_id: str,
    result: object,
) -> tuple[str | None, str | None]:
    """Resolve the scientific input edge without confusing it with a retry edge.

    Retry attempts use ``parent_run_id`` to point to the failed attempt.  Fresh
    smoke stages historically recorded their scientific predecessor in the
    immutable spec parameter ``upstream_run_id`` instead.  Prefer that explicit
    scientific edge and retain the ordinary parent as an execution-lineage
    field in the report.
    """
    explicit: str | None = None
    try:
        spec = registry.load_spec(run_id)
    except (AttributeError, FileNotFoundError):
        spec = None
    if spec is not None:
        candidate = spec.parameters.get("upstream_run_id")
        if candidate is not None:
            if not isinstance(candidate, str) or not candidate:
                raise ValueError(f"invalid upstream_run_id in immutable spec: {run_id}")
            explicit = candidate
    if explicit:
        upstream = registry.load_result(explicit)
        if upstream.route != result.route or upstream.project_id != result.project_id:
            raise ValueError(
                f"scientific upstream crosses route/project boundary: {run_id} -> {explicit}"
            )
        return explicit, "spec.parameters.upstream_run_id"
    parent = result.parent_run_id
    return parent, "result.parent_run_id" if parent else None


def _write_traceability_report(
    project_root: Path,
    parent_run_id: str,
    current_run_id: str,
    chain_config: Path,
    output_dir: Path,
    pareto_manifest: dict[str, object],
) -> tuple[Path, Path]:
    project_root = project_root.resolve()
    chain_config = chain_config.resolve()
    output_dir = output_dir.resolve()
    registry = ArtifactRegistry(project_root, create=False)
    stages: list[dict[str, object]] = []
    seen: set[str] = set()
    run_id: str | None = parent_run_id
    while run_id:
        if run_id in seen:
            raise ValueError("cycle detected in Ligand-first parent run chain")
        seen.add(run_id)
        result = registry.load_result(run_id)
        if result.route != "ligand-first":
            raise ValueError(f"non-Ligand-first run found in trace chain: {run_id}")
        scientific_upstream_run_id, lineage_edge = _scientific_upstream(
            registry, run_id, result
        )
        manifests: list[dict[str, str]] = []
        artifact_dir = registry.job_dir(run_id) / "artifacts"
        for path in sorted(artifact_dir.glob("*.json")):
            if "manifest" in path.name or path.name == "ligand_first_candidates.json":
                manifests.append(
                    {
                        "path": str(path.relative_to(project_root)),
                        "sha256": sha256_file(path),
                    }
                )
        stages.append(
            {
                "run_id": run_id,
                "engine": result.engine,
                "state": result.state,
                "return_code": result.return_code,
                "parent_run_id": result.parent_run_id,
                "scientific_upstream_run_id": scientific_upstream_run_id,
                "lineage_edge": lineage_edge,
                "submitted_at": result.submitted_at,
                "finished_at": result.finished_at,
                "candidate_table_path": result.candidate_table_path,
                "execution_attempts": _retry_attempts(
                    registry, run_id, result, lineage_edge
                ),
                "manifests": manifests,
            }
        )
        run_id = scientific_upstream_run_id
    stages.reverse()
    selection_gates = pareto_manifest.get("quality_gates", {})
    selection_passed = (
        pareto_manifest.get("status") == "completed"
        and bool(selection_gates)
        and all(value is True for value in selection_gates.values())
    )
    upstream_passed = all(stage["state"] == "COMPLETED" and stage["return_code"] == 0
                          for stage in stages)
    stages.append(
        {
            "run_id": current_run_id,
            "engine": "ligand-first-pareto",
            "state": "COMPLETED" if selection_passed else "QUALITY_GATE_FAILED",
            "parent_run_id": parent_run_id,
            "manifest": "stage6_pareto_manifest.json",
            # The Pareto result is being assembled by this process and may not
            # yet be committed to the registry. Record the observed outcome,
            # but leave runtime/resources absent rather than inventing them.
            "execution_attempts": [
                {
                    "run_id": current_run_id,
                    "state": "COMPLETED" if selection_passed else "QUALITY_GATE_FAILED",
                    "return_code": 0 if selection_passed else 4,
                    "runtime_seconds": None,
                    "requested_resources": None,
                    "resource_consumption": None,
                }
            ],
        }
    )
    observed_attempts = [
        attempt
        for stage in stages
        for attempt in stage.get("execution_attempts", [])
    ]
    completed_attempts = sum(
        attempt.get("state") == "COMPLETED" and attempt.get("return_code") == 0
        for attempt in observed_attempts
    )
    failed_attempts = len(observed_attempts) - completed_attempts
    total_runtime_seconds = sum(
        float(attempt["runtime_seconds"])
        for attempt in observed_attempts
        if attempt.get("runtime_seconds") is not None
    )
    resource_consumption = {
        key: sum(
            float(attempt["resource_consumption"][key])
            for attempt in observed_attempts
            if attempt.get("resource_consumption") is not None
        )
        for key in ("cpu_hours", "gpu_hours", "memory_gb_hours")
    }
    execution_evidence = {
        "observed_attempt_count": len(observed_attempts),
        "runtime_observed_attempt_count": sum(
            attempt.get("runtime_seconds") is not None for attempt in observed_attempts
        ),
        "resource_observed_attempt_count": sum(
            attempt.get("resource_consumption") is not None for attempt in observed_attempts
        ),
        "completed_attempt_count": completed_attempts,
        "failed_attempt_count": failed_attempts,
        "attempt_failure_rate": (
            failed_attempts / len(observed_attempts) if observed_attempts else None
        ),
        "total_observed_runtime_seconds": total_runtime_seconds,
        "requested_resource_consumption": resource_consumption,
        "monetary_cost": None,
        "monetary_cost_reason": "not_available_without_an_approved_rate_card",
        "repeatability": "not_assessed_single_chain",
    }
    payload = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "route": "ligand-first",
        "status": "completed" if selection_passed and upstream_passed else "incomplete",
        "chain_config": {
            "path": str(chain_config.relative_to(project_root)),
            "sha256": sha256_file(chain_config),
        },
        "stage_count": len(stages),
        "stages": stages,
        "execution_evidence": execution_evidence,
        "final_selection": pareto_manifest.get("quality", {}),
        "interpretation": (
            "Computational prioritization only; potency, target engagement, selectivity, "
            "kinetics, synthesis and safety require experimental validation."
        ),
    }
    json_path = output_dir / "ligand_first_traceability_report.json"
    md_path = output_dir / "ligand_first_traceability_report.md"
    temporary = json_path.with_name(f".{json_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(json_path)
    lines = [
        "# Ligand-first traceability report",
        "",
        f"- Chain config: `{payload['chain_config']['path']}`",
        f"- Registered stages: {len(stages)}",
        f"- Report status: {payload['status']}",
        f"- Final selected: {payload['final_selection'].get('selected_count', 0)}",
        f"- Observed attempts: {execution_evidence['observed_attempt_count']}",
        f"- Failed attempts: {execution_evidence['failed_attempt_count']}",
        f"- Observed runtime: {execution_evidence['total_observed_runtime_seconds']:.1f} s",
        f"- Requested CPU-hours: {execution_evidence['requested_resource_consumption']['cpu_hours']:.3f}",
        f"- Requested GPU-hours: {execution_evidence['requested_resource_consumption']['gpu_hours']:.3f}",
        f"- Repeatability: {execution_evidence['repeatability']}",
        "",
        "| Stage | Engine | Run ID | State |",
        "|---:|---|---|---|",
    ]
    lines.extend(
        f"| {index} | {stage['engine']} | `{stage['run_id']}` | {stage['state']} |"
        for index, stage in enumerate(stages, start=1)
    )
    lines.extend(
        [
            "",
            "All upstream manifests are recorded with SHA-256 in the JSON report.",
            "The ranking is computational prioritization and is not experimental proof.",
            "",
        ]
    )
    md_path.write_text("\n".join(lines))
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Select Library-first experimental candidates by non-dominated sorting."
    )
    parser.add_argument("--candidate-table", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--final-n", type=int, default=30)
    parser.add_argument(
        "--profile",
        choices=["library-first", "ligand-first"],
        default="library-first",
    )
    parser.add_argument(
        "--objective",
        action="append",
        default=[],
        help="Optional numeric objective as COLUMN:min or COLUMN:max.",
    )
    parser.add_argument("--trace-project-root", type=Path)
    parser.add_argument("--trace-parent-run-id")
    parser.add_argument("--trace-current-run-id")
    parser.add_argument("--trace-chain-config", type=Path)
    args = parser.parse_args()
    objectives: dict[str, str] = {}
    for raw in args.objective:
        try:
            column, direction = raw.rsplit(":", 1)
        except ValueError as error:
            raise SystemExit(f"invalid --objective {raw!r}; expected COLUMN:min|max") from error
        if not column or direction not in {"min", "max"}:
            raise SystemExit(f"invalid --objective {raw!r}; expected COLUMN:min|max")
        objectives[column] = direction
    frame = (
        pd.read_parquet(args.candidate_table)
        if args.candidate_table.suffix.lower() == ".parquet"
        else pd.read_csv(args.candidate_table)
    )
    manifest = select_pareto_candidates(
        frame,
        args.output_dir,
        final_n=args.final_n,
        additional_objectives=objectives,  # type: ignore[arg-type]
        profile=args.profile,
    )
    trace_args = (
        args.trace_project_root,
        args.trace_parent_run_id,
        args.trace_current_run_id,
        args.trace_chain_config,
    )
    if args.profile == "ligand-first":
        if not all(trace_args):
            raise SystemExit("Ligand-first profile requires all --trace-* arguments")
        json_report, md_report = _write_traceability_report(
            args.trace_project_root.resolve(),
            args.trace_parent_run_id,
            args.trace_current_run_id,
            args.trace_chain_config.resolve(),
            args.output_dir.resolve(),
            manifest,
        )
        manifest["outputs"].update(
            {
                path.name: {"path": str(path), "sha256": sha256_file(path)}
                for path in (json_report, md_report)
            }
        )
        manifest_path = args.output_dir / "stage6_pareto_manifest.json"
        temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(manifest_path)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "completed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
