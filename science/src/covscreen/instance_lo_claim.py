"""One-shot execution of a signed Ligand LO recipe on the named CPU instance.

Keep this supervisor free of scientific imports that start allocator threads.
The signed scientific command runs in a child contained by InstanceProcessTree.
"""
from pathlib import Path
import subprocess
import time

from .jobs import ArtifactRegistry, sha256_file


def validate_boundary(spec, instance='asgbie3'):
    if spec.route == 'library-first':
        from .library_instance import validate_lo
        validate_lo(spec, instance)
        return
    p, b = spec.parameters, spec.resource_budget
    count = p.get('expected_input_count')
    batch = spec.route == 'de-novo' and p.get('dispatch_policy') == 'denovo-batch-lo-instance-v1'
    prefix = 'de-flow-' if batch else 'ligand-flow-'
    from .ligand_instance import selected_instance
    expected_instance = 'asgbie3' if batch else selected_instance()
    if (instance != expected_instance or spec.engine != 'schrodinger-covdock-lo'
            or (not batch and spec.route != 'ligand-first') or not p.get('workflow_id', '').startswith(prefix)
            or p.get('execution_instance') != instance
            or (not batch and p.get('dispatch_policy') != 'ligand-lo-instance-v1')
            or (batch and not p.get('denovo_batch_lo_sha256'))
            or spec.provenance.get('worker_backend') != 'external-instance'
            or not ((spec.attempt == 1 and spec.parent_run_id is None)
                or (batch and spec.attempt == 2 and bool(spec.parent_run_id)))
            or type(count) is not int or not 1 <= count <= (12 if batch else 6)
            or b.max_candidates != count or b.gpu_count != 0
            or b.cpu_count != min(count, 12 if batch else 4) or b.memory_gb != (max(8, 4 * count) if batch else 8) or b.max_runtime_minutes != 240
            or p.get('njobs') != b.cpu_count or p.get('host') != f'localhost:{b.cpu_count}'
            or p.get('input_row_count') != count or p.get('final_n') != count
            or p.get('input_score_source') != 'core' or p.get('resume') is not False):
        raise ValueError('LO instance recipe differs from its bounded native plan')


def validate(root, spec, instance):
    from .native_approval import verify_spec
    from .source_release import verify_source, verify_input
    verify_spec(spec)
    validate_boundary(spec, instance)
    batch = spec.route == 'de-novo' and spec.parameters.get('dispatch_policy') == 'denovo-batch-lo-instance-v1'
    if (spec.execution_mode != 'execute' or not spec.human_approved
            or (not batch and spec.parameters.get('environment_eligible_for_exploratory_refinement') is not True)):
        raise ValueError('LO instance execution requires its approved environment-bound plan')
    verify_source(spec)
    if batch:
        from .layout import enable_de_novo
        enable_de_novo()
        from covscreen_de_novo.batch_lo import validate_compiled_spec
        validate_compiled_spec(root, spec)
    for name, digest in spec.input_sha256.items():
        verify_input(root, name, digest, spec)


def inspect(root: Path, run_id: str, instance: str) -> dict:
    registry = ArtifactRegistry(root)
    job = registry.job_dir(run_id)
    digest = sha256_file(job / 'spec.json')
    spec = registry.load_spec(run_id)
    validate(root, spec, instance)
    result = registry.load_result(run_id)
    if (spec.run_id != run_id or any(getattr(result, key) != getattr(spec, key)
            for key in ('run_id', 'engine', 'project_id', 'route', 'attempt', 'parent_run_id'))
            or sha256_file(job / 'spec.json') != digest):
        raise ValueError('LO instance task identity changed')
    return {'run_id': run_id, 'spec_sha256': digest, 'execution_instance': instance,
            'engine': spec.engine, 'state': result.state,
            'worker_claimed': (job / 'instance_worker_claim').is_dir()}


def claim(root, run_id, expected_digest, instance):
    from .instance_claim import inspect_isolated
    record = inspect_isolated(root, run_id, instance)
    if record['spec_sha256'] != expected_digest or record['state'] != 'QUEUED':
        raise ValueError('LO instance claim requires the exact queued task')
    marker = ArtifactRegistry(root).job_dir(run_id) / 'instance_worker_claim'
    marker.mkdir()  # Atomic, permanent, shared across all launch attempts.
    return marker


def execute(root, run_id, expected_digest, instance):
    from .execution import _execute_claimed_job, _engine_environment
    from .instance_process import InstanceProcessTree, InstanceProcessCancelled, InstanceRuntimeExceeded
    from .job_cancellation import requested, JobCancelled
    root = Path(root).resolve(strict=True)
    tree = InstanceProcessTree()
    claim(root, run_id, expected_digest, instance)

    def run(registry, spec):
        from .instance_claim import inspect_isolated
        record = inspect_isolated(root, run_id, instance)
        job = registry.job_dir(run_id)
        if record['spec_sha256'] != expected_digest or sha256_file(job / 'spec.json') != expected_digest:
            raise ValueError('LO instance task changed after claim')
        seconds = spec.resource_budget.max_runtime_minutes * 60
        env = _engine_environment(spec, root)
        # Parallelism is the bounded vendor subjob count, not threads per subjob.
        for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
            env[key] = '1'
        try:
            with (job / 'stdout.log').open('w') as out, (job / 'stderr.log').open('w') as err:
                return tree.run(spec.command, deadline=time.monotonic() + seconds,
                    cancelled=lambda: requested(registry, run_id), cpu_count=spec.resource_budget.cpu_count,
                    cwd=spec.working_directory, env=env, stdin=subprocess.DEVNULL,
                    stdout=out, stderr=err, start_new_session=True)
        except InstanceProcessCancelled as error:
            raise JobCancelled('LO instance process tree stopped') from error
        except InstanceRuntimeExceeded as error:
            raise subprocess.TimeoutExpired(spec.command, seconds) from error

    return _execute_claimed_job(root, run_id, instance_runner=run)
