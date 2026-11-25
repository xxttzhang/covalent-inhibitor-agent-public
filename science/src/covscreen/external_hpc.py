"""Host-approved bounded CovDock jobs: reserve once, execute on HPC, keep evidence."""
from __future__ import annotations

import json
from pathlib import Path

from .jobs import ArtifactRegistry, sha256_file, utc_now
from .covdock_retained import RETAINED_POLICY, validate_retained_spec


SHARDED_POLICY = "ligand-hpc-sharded-v1"


def validate_boundary(spec) -> None:
    """Validate the resource/concurrency envelope before native sealing."""
    budget = spec.resource_budget
    if spec.engine == 'schrodinger-covdock-lo' and spec.parameters.get('dispatch_policy') == 'library-vs-lo-v1':
        from .library_lo_input import BUDGET
        count = spec.parameters.get('expected_input_count')
        if (spec.route != 'library-first' or not spec.project_id.startswith('library-flow-')
            or spec.parameters.get('workflow_id') != spec.project_id
            or spec.execution_mode != 'execute' or not spec.human_approved
            or spec.provenance.get('worker_backend') != 'external-hpc'
            or spec.attempt != 1 or spec.parent_run_id is not None
            or type(count) is not int or not 1 <= count <= 5
            or budget.model_dump(mode='json') != {**BUDGET, 'max_candidates': count}
            or spec.parameters.get('input_row_count') != count or spec.parameters.get('final_n') != count
            or spec.parameters.get('njobs') != min(count, 4) or spec.parameters.get('host') != 'localhost:4'
            or spec.parameters.get('input_score_source') != 'core' or spec.parameters.get('resume') is not False):
            raise ValueError('Library LO differs from its independently approved CPU subset budget')
        return
    if spec.engine == 'covindb-prescreen' and spec.parameters.get('vs_prescreen'):
        from .library_vs_prescreen_input import BUDGET, LIMITS
        if (spec.route != 'library-first' or not spec.project_id.startswith('library-flow-')
            or spec.execution_mode != 'execute' or not spec.human_approved
            or spec.provenance.get('worker_backend') != 'external-hpc'
            or spec.attempt != 1 or spec.parent_run_id is not None
            or budget.model_dump(mode='json') != BUDGET
            or set(spec.parameters) != {'vs_prescreen', 'target_config', 'source_sha256', *LIMITS}
            or any(spec.parameters.get(key) != value for key, value in LIMITS.items())):
            raise ValueError('VS prescreen differs from its approved 20000-record CPU pilot')
        return
    if spec.engine == 'schrodinger-covdock-vs':
        from .covdock_vs_preparation import Preparation
        packet = spec.parameters.get('vs_preparation', {})
        plan = Preparation.model_validate(packet.get('plan'))
        if (spec.route != plan.source.route or spec.project_id != plan.source.workflow_id
                or spec.execution_mode != 'execute' or not spec.human_approved
                or spec.provenance.get('worker_backend') != 'external-hpc'
                or spec.attempt != 1 or spec.parent_run_id is not None
                or budget.model_dump(mode='json') != plan.budget.model_dump(mode='json')
                or set(spec.parameters) != {'vs_preparation','input_row_count'}
                or spec.parameters['input_row_count'] != packet.get('candidate_count')
                or not 1 <= spec.parameters['input_row_count'] <= budget.max_candidates
                or not 1 <= plan.njobs <= min(budget.cpu_count,spec.parameters['input_row_count'])
                or packet.get('mode') != 'enrichment' or packet.get('worker_backend') != 'external-hpc'):
            raise ValueError('VS job differs from its approved CPU HPC recipe')
        return
    if (spec.engine != "schrodinger-covdock-lo" or spec.route != "ligand-first"
            or spec.execution_mode != "execute" or not spec.human_approved
            or spec.provenance.get("worker_backend") != "external-hpc"
            or not 1 <= budget.max_candidates <= 100 or budget.gpu_count != 0
            or not 1 <= budget.cpu_count <= 55 or budget.memory_gb > 300
            or budget.max_runtime_minutes > 360
            or not 1 <= spec.parameters.get("input_row_count", 0) <= budget.max_candidates):
        raise ValueError("Job is outside the approved bounded CPU HPC boundary")
    if spec.parameters.get('retained_candidates') and spec.parameters.get('dispatch_policy') != RETAINED_POLICY:
        raise ValueError('Retained candidates require their explicit recovery policy')
    if spec.parameters.get('dispatch_policy') == RETAINED_POLICY:
        p = spec.parameters
        if ((budget.gpu_count, budget.cpu_count, budget.memory_gb,
             budget.max_runtime_minutes, budget.max_candidates) != (0, 8, 32, 180, 3)
                or spec.attempt not in {3, 4} or not spec.parent_run_id
                or p.get('input_row_count') != 3 or p.get('checkpoint_size') != 1
                or p.get('checkpoint_workers') != 2 or p.get('checkpoint_worker_cpus') != 4
                or p.get('njobs') != 1 or p.get('host') != 'localhost:1'
                or p.get('resume') is not False
                or not isinstance(p.get('retained_candidates'), list)
                or len(p['retained_candidates']) != 1
                or any(p.get(key) for key in ('reuse_output_prefix', 'reuse_grouped_output_dir', 'reuse_artifact_dir'))):
            raise ValueError('Retained timeout recovery exceeds its exact approved boundary')
    if spec.parameters.get("dispatch_policy") == SHARDED_POLICY:
        parameters = spec.parameters
        exact_budget = (0, 12, 48, 180, 3)
        observed_budget = (
            budget.gpu_count,
            budget.cpu_count,
            budget.memory_gb,
            budget.max_runtime_minutes,
            budget.max_candidates,
        )
        if (observed_budget != exact_budget
                or parameters.get("input_row_count") != 3
                or parameters.get("checkpoint_size") != 1
                or parameters.get("checkpoint_workers") != 3
                or parameters.get("checkpoint_worker_cpus") != 4
                or parameters.get("njobs") != 1
                or parameters.get("host") != "localhost:1"
                or parameters.get("resume") is not False
                or any(parameters.get(key) for key in (
                    "reuse_output_prefix", "reuse_grouped_output_dir", "reuse_artifact_dir"
                ))):
            raise ValueError("Sharded timeout recovery exceeds its exact approved boundary")


def _same_scientific_inputs(root: Path, parent, child) -> bool:
    for key in ("candidate_table", "receptor", "pose_consistency_table"):
        name = parent.parameters.get(key)
        if name is None:
            if child.parameters.get(key) is not None:
                return False
            continue
        if child.parameters.get(key) != name:
            return False
        digest = parent.input_sha256.get(name)
        if not digest or child.input_sha256.get(name) != digest:
            return False
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or sha256_file(path) != digest:
            return False
    return True


def _is_sharded_timeout_recovery(root: Path, registry: ArtifactRegistry, spec) -> bool:
    if spec.parameters.get("dispatch_policy") != SHARDED_POLICY:
        return False
    if spec.attempt != 3 or not spec.parent_run_id:
        return False
    parent = registry.load_spec(spec.parent_run_id)
    parent_result = registry.load_result(spec.parent_run_id)
    from .external_inspire import inspect as inspect_inspire
    parent_record = inspect_inspire(root, parent.run_id)
    expected_parameters = dict(parent.parameters)
    expected_parameters.update(
        dispatch_policy=SHARDED_POLICY,
        checkpoint_size=1,
        checkpoint_workers=3,
        checkpoint_worker_cpus=4,
        njobs=1,
        host="localhost:1",
        resume=False,
    )
    return bool(
        parent.attempt == 2
        and parent.provenance.get("worker_backend") == "external-inspire"
        and parent.parameters.get("dispatch_policy") == "ligand-smoke-v1"
        and parent_result.state == "TIMED_OUT"
        and parent_result.failure_reason == "external_scheduler_timed_out"
        and parent_record["state"] == "TIMED_OUT"
        and spec.request_id == parent.request_id + "-retry-3"
        and spec.project_id == parent.project_id
        and spec.parameters == expected_parameters
        and _same_scientific_inputs(root, parent, spec)
    )


def pending(root: Path) -> list[dict]:
    records = []
    for path in sorted((root / "state/jobs").glob("*/result.json")):
        result = json.loads(path.read_text())
        if result.get("state") not in {"QUEUED", "RUNNING"}:
            continue
        try:
            record = inspect(root, path.parent.name)
        except ValueError:
            continue  # Unrelated jobs are never dispatched by this bounded bridge.
        records.append(record)
    return records


def inspect(root: Path, run_id: str) -> dict:
    root = Path(root).resolve()
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run_id)
    result = registry.load_result(run_id)
    budget = spec.resource_budget
    from .native_approval import verify_spec
    verify_spec(spec)
    validate_boundary(spec)
    if spec.parameters.get('dispatch_policy') == RETAINED_POLICY:
        validate_retained_spec(root, spec)
        from .external_inspire import inspect as inspect_inspire
        if spec.attempt == 4:
            if inspect(root, spec.parent_run_id)['state'] != 'FAILED':
                raise ValueError('Storage recovery parent is not failed')
        elif inspect_inspire(root, spec.parent_run_id)['state'] != 'TIMED_OUT':
            raise ValueError('Retained recovery parent is not terminal')
        request_matches = True
    else:
        request_matches = _is_sharded_timeout_recovery(root, registry, spec)
    if not request_matches and bool(spec.request_id) and spec.attempt == 1:
        request_matches = True
    if (not request_matches and spec.parent_run_id and spec.attempt in {2, 3}
            and spec.parameters.get("dispatch_policy") not in {SHARDED_POLICY, RETAINED_POLICY}):
        parent = registry.load_spec(spec.parent_run_id)
        parent_result = registry.load_result(spec.parent_run_id)
        request_matches = (
            parent.attempt == spec.attempt - 1
            and spec.request_id == parent.request_id + f"-retry-{spec.attempt}"
            and parent_result.state in {"FAILED", "CANCELLED", "TIMED_OUT"}
            and spec.input_sha256 == parent.input_sha256
            and spec.parameters == parent.parameters
            and spec.resource_budget == parent.resource_budget
            and spec.project_id == parent.project_id
        )
        if request_matches:
            inspect(root, spec.parent_run_id)  # Verify the bounded ancestry back to the original approval.
    if not request_matches:
        raise ValueError("Job ancestry is outside the approved bounded CPU HPC boundary")
    path = registry.job_dir(run_id) / "spec.json"
    receipt = registry.job_dir(run_id) / "external_hpc.json"
    return {"run_id": run_id, "engine": spec.engine, "route": spec.route,
            "state": result.state, "spec_sha256": sha256_file(path),
            "name": ("prescreen-one-" if spec.engine == 'covindb-prescreen' else
                     "vs-one-" if spec.engine == 'schrodinger-covdock-vs' else "ligand-one-") + sha256_file(path)[:20],
            "minutes": budget.max_runtime_minutes,
            "budget": budget.model_dump(mode='json'),
            "receipt": json.loads(receipt.read_text()) if receipt.exists() else None}


def reserve(root: Path, run_id: str, expected: str) -> dict:
    from .denovo_dispatch_recovery import dispatch_lock
    with dispatch_lock(ArtifactRegistry(root), run_id):
        return _reserve_locked(root, run_id, expected)


def _reserve_locked(root: Path, run_id: str, expected: str) -> dict:
    record = inspect(root, run_id)
    if record["spec_sha256"] != expected or record["state"] != "QUEUED":
        raise ValueError("Dispatch requires the exact approved queued spec")
    registry = ArtifactRegistry(root)
    path = registry.job_dir(run_id) / "external_hpc.json"
    payload = {"name": record["name"], "spec_sha256": expected, "reserved_at": utc_now()}
    # Exclusive creation is the at-most-once submission boundary. An uncertain
    # response must be reconciled by name, never automatically submitted again.
    with path.open("x") as handle:
        json.dump(payload, handle)
    return payload


def run(root: Path, run_id: str, expected: str):
    from .execution import execute_persisted_job

    record = inspect(root, run_id)
    if (record["spec_sha256"] != expected or record["state"] != "QUEUED"
            or not record["receipt"] or record["receipt"]["spec_sha256"] != expected):
        raise ValueError("Missing reservation, changed spec, or job already started")
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run_id)
    from .source_release import verify_input, verify_source
    verify_source(spec)
    from .stage_boundary import require_prescreen_parent
    require_prescreen_parent(root, spec)
    for name, digest in spec.input_sha256.items():
        verify_input(root, name, digest, spec)
    (registry.job_dir(run_id) / "external_worker_claim").mkdir()
    return execute_persisted_job(root, run_id)


def fail(root: Path, run_id: str, expected: str, name: str, state: str) -> dict:
    record = inspect(root, run_id)
    if (record["spec_sha256"] != expected or record["name"] != name
            or not record["receipt"] or state not in {"FAILED", "CANCELLED", "TIMED_OUT"}):
        raise ValueError("Exact reserved scheduler terminal state required")
    registry = ArtifactRegistry(root)
    result = registry.load_result(run_id)
    if result.state in {"QUEUED", "RUNNING"}:
        result.state = state
        result.finished_at = utc_now()
        result.failure_reason = "external_scheduler_" + state.lower()
        result.message = "HPC terminated; retained outputs require inspection. No automatic retry."
        registry.write_result(result)
    return {"run_id": run_id, "state": result.state}
