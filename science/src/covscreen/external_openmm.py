"""One native-approved Ligand-first CUDA equilibration, with no automatic retry."""
import json
import re
from pathlib import Path

from .jobs import ArtifactRegistry, sha256_file, utc_now
from .native_approval import verify_spec


def configured_profile(runtime):
    profile = runtime.get('scheduler_profile')
    fields = {'workspace', 'project', 'group', 'quota', 'image'}
    if (not isinstance(profile, dict) or set(profile) != fields
            or any(not isinstance(v, str) or not v.strip() or any(ord(c) < 32 for c in v)
                   for v in profile.values())
            or profile['workspace'] != '分布式训练空间'
            or profile['quota'] != '1,20,200'):
        raise ValueError('CUDA equilibration requires an explicit GPU scheduler profile with quota 1,20,200')
    return dict(profile)


def validate_budget(budget):
    if ((budget.gpu_count, budget.cpu_count, budget.memory_gb, budget.max_candidates) != (1, 20, 200, 1)
            or not 0 < budget.max_runtime_minutes <= 120):
        raise ValueError('CUDA equilibration requires exact allocated quota 1 GPU, 20 CPU, 200 GiB, one candidate, at most 120 minutes')


def validate_boundary(spec):
    p = spec.parameters
    if (spec.engine != 'library-first-openmm-equilibration' or spec.route != 'library-first'
            or spec.provenance.get('worker_backend') != 'external-openmm'
            or not re.fullmatch(r'ligand-flow-[a-f0-9]{24}', str(p.get('workflow_id', '')))
            or not spec.parent_run_id or spec.parent_run_id != p.get('short_qc_run_id')
            or p.get('platform') != 'CUDA' or p.get('cpu_threads') != 4
            or p.get('duration_ps') != 500 or p.get('report_interval_ps') != 10
            or p.get('platform_validation_required') is not True
            or p.get('platform_properties') != {'Precision': 'mixed', 'DeviceIndex': '0',
                 'UseCpuPme': 'false', 'DeterministicForces': 'true'}):
        raise ValueError('Outside registered single-candidate CUDA equilibration boundary')
    validate_budget(spec.resource_budget)
    configured_profile({'scheduler_profile': p.get('scheduler_profile')})


def inspect(root, run_id):
    registry = ArtifactRegistry(root)
    spec, result = registry.load_spec(run_id), registry.load_result(run_id)
    verify_spec(spec)
    validate_boundary(spec)
    if spec.execution_mode != 'execute' or not spec.human_approved:
        raise ValueError('Native-approved execution required')
    digest = sha256_file(registry.job_dir(run_id) / 'spec.json')
    path = registry.job_dir(run_id) / 'external_openmm.json'
    return {'run_id': run_id, 'spec_sha256': digest, 'name': 'ligand-equil-' + digest[:20],
        'state': result.state, 'engine': spec.engine, 'workload': 'job',
        'workflow_id': spec.parameters['workflow_id'], 'attempt': spec.attempt,
        'scheduler_profile': spec.parameters['scheduler_profile'],
        'budget': spec.resource_budget.model_dump(mode='json'), 'engine_cpu_threads': 4,
        'minutes': spec.resource_budget.max_runtime_minutes,
        'receipt': json.loads(path.read_text()) if path.exists() else None}


def _claim(root, record):
    return Path(root) / 'state/external_openmm' / record['workflow_id'] / (str(record['attempt']) + '.json')


def reserve(root, run_id, expected):
    record = inspect(root, run_id)
    if record['spec_sha256'] != expected or record['state'] != 'QUEUED' or record['receipt']:
        raise ValueError('Exact unreserved queued equilibration required')
    payload = {key: record[key] for key in ('run_id', 'spec_sha256', 'name', 'workflow_id', 'attempt', 'scheduler_profile')}
    payload['reserved_at'] = utc_now()
    claim = _claim(root, record)
    claim.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive workflow-attempt claim also prevents dispatch under another request ID.
    # A crash leaves the claim for review; it never authorizes another create.
    with claim.open('x') as stream:
        json.dump(payload, stream)
    with (ArtifactRegistry(root).job_dir(run_id) / 'external_openmm.json').open('x') as stream:
        json.dump(payload, stream)
    return payload


def run(root, run_id, expected):
    root = Path(root).resolve()
    record = inspect(root, run_id)
    receipt = record['receipt']
    if (record['spec_sha256'] != expected or record['state'] != 'QUEUED'
            or not receipt or receipt['spec_sha256'] != expected or receipt['run_id'] != run_id
            or json.loads(_claim(root, record).read_text()) != receipt):
        raise ValueError('Missing reservation, changed spec or already started')
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run_id)
    from .source_release import verify_source, verify_input
    verify_source(spec)
    for name, digest in spec.input_sha256.items():
        verify_input(root, name, digest, spec)
    (registry.job_dir(run_id) / 'external_worker_claim').mkdir()
    from .execution import execute_persisted_job
    return execute_persisted_job(root, run_id)


def fail(root, run_id, expected, name, state):
    record = inspect(root, run_id)
    if (record['spec_sha256'] != expected or record['name'] != name or not record['receipt']
            or state not in {'FAILED', 'CANCELLED', 'TIMED_OUT'}):
        raise ValueError('Exact reserved scheduler terminal observation required')
    registry = ArtifactRegistry(root)
    result = registry.load_result(run_id)
    if result.state in {'QUEUED', 'RUNNING'}:
        result.state, result.finished_at = state, utc_now()
        result.failure_reason = 'external_scheduler_' + state.lower()
        result.message = 'Scheduler terminated; partial evidence retained; no automatic retry.'
        registry.write_result(result)
    return {'run_id': run_id, 'state': result.state}
