"""One-shot native-approved De novo Glide execution on asgbie3."""
from pathlib import Path
import subprocess
import os
import sys
import time
from .jobs import ArtifactRegistry, sha256_file


def validate(root, spec, instance):
    from .native_approval import verify_spec
    from .source_release import verify_source, verify_input
    from .layout import enable_de_novo
    enable_de_novo()
    from covscreen_de_novo.batch_glide import validate_boundary, request_for, validate_compiled_spec
    verify_spec(spec)
    verify_source(spec)
    validate_boundary(spec, instance)
    validate_compiled_spec(root, spec)
    if spec.execution_mode != 'execute' or not spec.human_approved:
        raise ValueError('Glide requires native execution approval')
    p = spec.parameters
    expected = request_for(root, p['prereaction_source_sha256'], p['prereaction_manifest_sha256'], 'execute')
    if (expected.request_id != spec.request_id or expected.project_id != spec.project_id
            or expected.resource_budget != spec.resource_budget
            or any(p.get(key) != value for key, value in expected.parameters.items())):
        raise ValueError('Glide task differs from frozen independent inputs')
    for name, digest in spec.input_sha256.items():
        verify_input(root, name, digest, spec)


def inspect(root, run_id, instance):
    registry = ArtifactRegistry(root)
    path = registry.job_dir(run_id) / 'spec.json'
    digest = sha256_file(path)
    spec, result = registry.load_spec(run_id), registry.load_result(run_id)
    validate(root, spec, instance)
    if (spec.run_id != run_id or any(getattr(result, key) != getattr(spec, key)
            for key in ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))
            or sha256_file(path) != digest):
        raise ValueError('Glide instance task identity changed')
    return {'run_id': run_id, 'spec_sha256': digest, 'execution_instance': instance,
        'engine': spec.engine, 'state': result.state,
        'worker_claimed': (path.parent / 'instance_worker_claim').is_dir()}


def execute(root, run_id, expected_digest, instance):
    from .execution import _execute_claimed_job
    from .instance_process import InstanceProcessTree, InstanceProcessCancelled, InstanceRuntimeExceeded
    from .job_cancellation import JobCancelled
    root = Path(root).resolve(strict=True)
    from .instance_claim import inspect_isolated
    record = inspect_isolated(root, run_id, instance)
    if record['spec_sha256'] != expected_digest or record['state'] != 'QUEUED':
        raise ValueError('Glide claim requires the exact queued task')
    registry = ArtifactRegistry(root)
    (registry.job_dir(run_id) / 'instance_worker_claim').mkdir()
    tree = InstanceProcessTree()
    def run(registry, spec):
        record = inspect_isolated(root, run_id, instance)
        if record['spec_sha256'] != expected_digest or sha256_file(registry.job_dir(run_id) / 'spec.json') != expected_digest:
            raise ValueError('Glide task changed after claim')
        from .job_cancellation import requested
        try:
            program = ('from covscreen.layout import enable_de_novo; enable_de_novo(); '
                       'import runpy; runpy.run_module("covscreen_de_novo.batch_glide_runner",run_name="__main__")')
            return tree.run([sys.executable, '-B', '-c', program,
                '--root', str(root), '--run-id', run_id],
                deadline=time.monotonic() + spec.resource_budget.max_runtime_minutes * 60,
                cancelled=lambda: requested(registry, run_id), cpu_count=spec.resource_budget.cpu_count,
                cwd=root, env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)}, stdin=subprocess.DEVNULL,
                start_new_session=True)
        except InstanceProcessCancelled as error:
            raise JobCancelled('Glide instance process tree stopped') from error
        except InstanceRuntimeExceeded as error:
            raise subprocess.TimeoutExpired(spec.command, spec.resource_budget.max_runtime_minutes * 60) from error
    return _execute_claimed_job(root, run_id, instance_runner=run)
