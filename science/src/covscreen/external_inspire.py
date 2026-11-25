"""Opt-in three-candidate acceptance dispatcher. Not a production-scale policy.

Native per-stage approval is mandatory. Reviewed AF3 preparation failures and
CovDock parallel-progress failures may have one separately approved recovery.
Original claims remain immutable; CovDock recovery is restricted to serial work.
"""
import json
import re
from pathlib import Path

from .jobs import ArtifactRegistry, sha256_file, utc_now
from .native_approval import verify_spec

# gpu, cpu, GiB, minutes; CPU evaluation + Pareto together <=30 minutes.
STAGES = {
    'carsidock-cov': (1, 20, 200, 30),
    'alphafold3-mpae': (1, 20, 200, 90),
    'schrodinger-covdock-lo': (0, 4, 16, 180),
    'ligand-first-evaluation': (0, 4, 16, 15),
    'ligand-first-pareto': (0, 2, 16, 15),
}


def validate_boundary(spec):
    p, b = spec.parameters, spec.resource_budget
    if (spec.engine not in STAGES or spec.route != 'ligand-first'
            or spec.provenance.get('worker_backend') != 'external-inspire'
            or p.get('dispatch_policy') != 'ligand-smoke-v1'
            or not re.fullmatch(r'ligand-smoke-[a-z0-9-]{1,64}', str(p.get('dispatch_batch', '')))
            or not (spec.attempt == 1 and not spec.parent_run_id
                    or spec.attempt == 2 and spec.parent_run_id
                    and spec.engine in {'alphafold3-mpae', 'schrodinger-covdock-lo'})
            or not 1 <= b.max_candidates <= 3
            or not 1 <= p.get('input_row_count', 0) <= b.max_candidates):
        raise ValueError('Outside three-candidate Ligand-first smoke boundary')
    gpu, cpu, mem, minutes = STAGES[spec.engine]
    # Exact quota avoids silent rounding above native approval at submission.
    if (b.gpu_count, b.cpu_count, b.memory_gb) != (gpu, cpu, mem) or not 0 < b.max_runtime_minutes <= minutes:
        raise ValueError('Smoke resource budget exceeds or differs from approved quota')
    if any(p.get(key) for key in ('reuse_output_prefix', 'reuse_grouped_output_dir', 'reuse_artifact_dir')):
        raise ValueError('Fresh smoke calculations cannot reuse engine outputs')
    if (spec.attempt == 2 and spec.engine == 'schrodinger-covdock-lo'
            and (p.get('njobs') != 1 or p.get('host') != 'localhost:1'
                 or p.get('resume'))):
        raise ValueError('CovDock recovery requires fresh serial NJOBS=1 execution')


def claim_name(engine, attempt=1):
    return engine + ('.attempt2' if attempt == 2 else '') + '.json'


def validate_recovery(root, spec):
    if spec.attempt == 1:
        return
    registry = ArtifactRegistry(root)
    previous = registry.load_spec(spec.parent_run_id)
    verify_spec(previous)
    validate_boundary(previous)
    parameters = dict(previous.parameters)
    if spec.engine == 'schrodinger-covdock-lo':
        if not isinstance(parameters.get('njobs'), int) or parameters['njobs'] <= 1:
            raise ValueError('CovDock serial recovery requires a failed parallel parent')
        parameters.update(njobs=1, host='localhost:1')
    if (previous.attempt != 1 or previous.engine != spec.engine
            or registry.load_result(previous.run_id).state != 'FAILED'
            or parameters != spec.parameters or previous.project_id != spec.project_id
            or previous.resource_budget != spec.resource_budget
            or spec.request_id != previous.request_id + '-retry-2'
            or spec.provenance['native_approval'] == previous.provenance['native_approval']):
        raise ValueError('Recovery must separately approve the same failed scientific inputs and budget')
    folder = Path(root) / 'state/external_inspire_batches' / spec.parameters['dispatch_batch']
    original = json.loads((folder / claim_name(spec.engine)).read_text())
    if (original['run_id'] != previous.run_id
            or original['spec_sha256'] != sha256_file(registry.job_dir(previous.run_id) / 'spec.json')):
        raise ValueError('Recovery parent is not the original reserved attempt')
    # Runtime source fixes may change; scientific inputs and target cannot.
    keys = (('candidate_table', 'receptor') if spec.engine == 'schrodinger-covdock-lo'
            else ('candidate_table', 'source_data_json', 'target_config'))
    for key in keys:
        name = spec.parameters[key]
        if (name not in spec.input_sha256 or spec.input_sha256[name] != previous.input_sha256.get(name)
                or sha256_file(Path(root) / name) != spec.input_sha256[name]):
            raise ValueError('Recovery scientific input changed: ' + key)


def inspect(root, run_id):
    registry = ArtifactRegistry(root)
    spec, result = registry.load_spec(run_id), registry.load_result(run_id)
    verify_spec(spec)
    validate_boundary(spec)
    validate_recovery(root, spec)
    if spec.execution_mode != 'execute' or not spec.human_approved:
        raise ValueError('Native approved execute job required')
    digest = sha256_file(registry.job_dir(run_id) / 'spec.json')
    job_dir = registry.job_dir(run_id)
    path = job_dir / 'external_inspire.json'
    replacement_path = job_dir / 'external_inspire.scheduler-replacement1.json'
    replacement = json.loads(replacement_path.read_text()) if replacement_path.exists() else None
    original_name = 'ligand-smoke-' + digest[:20]
    dispatch_name = replacement['replacement_name'] if replacement else original_name
    eligible = bool(
        not replacement
        and result.state == 'FAILED'
        and result.failure_reason == 'external_scheduler_failed'
        and result.started_at is None
        and result.runtime_seconds is None
        and path.is_file()
        and not (job_dir / 'external_worker_claim').exists()
        and not (job_dir / 'artifacts').exists()
        and not (job_dir / 'stdout.log').exists()
        and not (job_dir / 'stderr.log').exists()
    )
    return {'run_id': run_id, 'engine': spec.engine, 'batch': spec.parameters['dispatch_batch'],
        'state': result.state, 'attempt': spec.attempt, 'parent_run_id': spec.parent_run_id,
        'spec_sha256': digest, 'name': dispatch_name, 'original_name': original_name,
        'workload': 'job' if spec.resource_budget.gpu_count else 'hpc',
        'minutes': spec.resource_budget.max_runtime_minutes,
        'budget': spec.resource_budget.model_dump(mode='json'),
        'receipt': json.loads(path.read_text()) if path.exists() else None,
        'scheduler_replacement': replacement,
        'scheduler_replacement_eligible': eligible,
        'scheduler_replacement_name': original_name + '-r1' if eligible else None}


def replace_failed_scheduler(root, run_id, expected, name, state):
    """Requeue one signed run after proof that no worker/science ever started.

    This is a scheduler placement replacement, not another scientific attempt:
    the signed spec, attempt number, inputs, budget and stage claim are unchanged.
    The terminal launcher failure is retained verbatim in an immutable receipt.
    """
    root = Path(root).resolve()
    record = inspect(root, run_id)
    from .docking_policy import require_engine_enabled
    require_engine_enabled(record['engine'])
    if (record['spec_sha256'] != expected or record['original_name'] != name
            or state != 'FAILED' or not record['scheduler_replacement_eligible']
            or not record['receipt'] or record['receipt']['name'] != name):
        raise ValueError('Exact pre-worker terminal scheduler failure required')
    registry = ArtifactRegistry(root)
    result = registry.load_result(run_id)
    job_dir = registry.job_dir(run_id)
    replacement = {
        'schema_version': '1.0',
        'run_id': run_id,
        'spec_sha256': expected,
        'original_name': name,
        'original_scheduler_state': state,
        'original_result': result.model_dump(mode='json'),
        'replacement_name': record['scheduler_replacement_name'],
        'reason': 'verified_pre_worker_scheduler_failure',
        'scientific_attempt_unchanged': True,
        'created_at': utc_now(),
    }
    replacement_path = job_dir / 'external_inspire.scheduler-replacement1.json'
    with replacement_path.open('x') as stream:
        json.dump(replacement, stream)
    result.state = 'QUEUED'
    result.finished_at = None
    result.failure_reason = None
    result.message = (
        'Same approved run requeued once after verified pre-worker scheduler failure; '
        'the original terminal evidence is retained in the scheduler replacement receipt.'
    )
    registry.write_result(result)
    return inspect(root, run_id)


def reserve(root, run_id, expected):
    root = Path(root)
    record = inspect(root, run_id)
    from .docking_policy import require_engine_enabled
    require_engine_enabled(record['engine'])
    if record['spec_sha256'] != expected or record['state'] != 'QUEUED' or record['receipt']:
        raise ValueError('Exact unreserved queued job required')
    registry = ArtifactRegistry(root)
    folder = root / 'state/external_inspire_batches' / record['batch']
    folder.mkdir(parents=True, exist_ok=True)
    stages = list(STAGES)
    for stage in stages[:stages.index(record['engine'])]:
        recovery = folder / claim_name(stage, 2)
        path = recovery if recovery.exists() else folder / claim_name(stage)
        if not path.is_file():
            raise ValueError('Previous stage has not been reserved: ' + stage)
        previous = json.loads(path.read_text())
        previous_record = inspect(root, previous['run_id'])
        if (previous_record['spec_sha256'] != previous['spec_sha256']
                or previous_record['batch'] != record['batch']
                or previous_record['state'] != 'COMPLETED'):
            raise ValueError('Previous stage is not verified complete; no parallelism or retry')
    payload = {key: record[key] for key in ('name', 'run_id', 'batch', 'engine', 'spec_sha256', 'attempt', 'parent_run_id')}
    payload['reserved_at'] = utc_now()
    # Exclusive batch-stage claim ALSO blocks duplicate jobs with different request IDs.
    # A crash between the two writes fails closed; no automatic removal or resubmit.
    with (folder / claim_name(record['engine'], record['attempt'])).open('x') as stream:
        json.dump(payload, stream)
    with (registry.job_dir(run_id) / 'external_inspire.json').open('x') as stream:
        json.dump(payload, stream)
    return payload


def run(root, run_id, expected):
    root = Path(root).resolve()
    record = inspect(root, run_id)
    receipt = record['receipt']
    if (record['spec_sha256'] != expected or record['state'] != 'QUEUED'
            or not receipt or receipt['spec_sha256'] != expected or receipt['run_id'] != run_id):
        raise ValueError('Missing reservation, changed spec or already started')
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run_id)
    claim = root / 'state/external_inspire_batches' / record['batch'] / claim_name(record['engine'], record['attempt'])
    if json.loads(claim.read_text()) != receipt:
        raise ValueError('Batch reservation differs from job reservation')
    from .source_release import verify_input, verify_source
    verify_source(spec)
    from .stage_boundary import require_prescreen_parent
    require_prescreen_parent(root, spec)
    for name, digest in spec.input_sha256.items():
        verify_input(root, name, digest, spec)
    (registry.job_dir(run_id) / 'external_worker_claim').mkdir()
    from .execution import execute_persisted_job
    return execute_persisted_job(root, run_id)


def fail(root, run_id, expected, name, state):
    record = inspect(root, run_id)
    if (record['spec_sha256'] != expected or record['name'] != name or not record['receipt']
            or state not in {'FAILED', 'CANCELLED', 'TIMED_OUT'}):
        raise ValueError('Exact reserved scheduler terminal state required')
    registry = ArtifactRegistry(root)
    result = registry.load_result(run_id)
    if result.state in {'QUEUED', 'RUNNING'}:
        result.state, result.finished_at = state, utc_now()
        result.failure_reason = 'external_scheduler_' + state.lower()
        result.message = 'Scheduler terminated; retained evidence requires review, no automatic retry.'
        registry.write_result(result)
    return {'run_id': run_id, 'state': result.state}
