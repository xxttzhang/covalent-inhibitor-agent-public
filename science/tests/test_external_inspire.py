import importlib.util
from pathlib import Path
import sys

import pytest

from covscreen import external_inspire as bridge
from covscreen.jobs import ArtifactRegistry, ResourceBudget
from test_external_hpc import approved, queued
from covscreen.jobs import sha256_file


def convert(root, run_id, engine='carsidock-cov', suffix=''):
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run_id)
    result = registry.load_result(run_id)
    gpu, cpu, memory, minutes = bridge.STAGES[engine]
    spec.engine = engine
    spec.run_id += suffix
    spec.parameters.update(dispatch_policy='ligand-smoke-v1', dispatch_batch='ligand-smoke-test', input_row_count=3)
    spec.provenance['worker_backend'] = 'external-inspire'
    spec.resource_budget = ResourceBudget(gpu_count=gpu, cpu_count=cpu, memory_gb=memory,
        max_candidates=3, max_runtime_minutes=minutes)
    result.run_id, result.engine, result.state = spec.run_id, engine, 'QUEUED'
    spec = approved(spec)
    if suffix:
        registry.create(spec, result)
    else:
        registry.write_spec(spec)
        registry.write_result(result)
    return spec


def test_requires_signed_exact_job_and_once_per_stage(queued):
    root, run_id = queued
    convert(root, run_id)
    record = bridge.inspect(root, run_id)
    bridge.reserve(root, run_id, record['spec_sha256'])
    with pytest.raises(ValueError, match='unreserved'):
        bridge.reserve(root, run_id, record['spec_sha256'])
    duplicate = convert(root, run_id, suffix='-duplicate')
    with pytest.raises(FileExistsError):
        bridge.reserve(root, duplicate.run_id, bridge.inspect(root, duplicate.run_id)['spec_sha256'])


def test_stages_are_serial_and_never_skip_failure(queued):
    root, run_id = queued
    convert(root, run_id)
    af3 = convert(root, run_id, 'alphafold3-mpae', '-af3')
    record = bridge.inspect(root, af3.run_id)
    with pytest.raises(ValueError, match='not been reserved'):
        bridge.reserve(root, af3.run_id, record['spec_sha256'])
    carsi = bridge.inspect(root, run_id)
    bridge.reserve(root, run_id, carsi['spec_sha256'])
    with pytest.raises(ValueError, match='not verified complete'):
        bridge.reserve(root, af3.run_id, record['spec_sha256'])
    registry = ArtifactRegistry(root)
    result = registry.load_result(run_id)
    result.state = 'COMPLETED'
    registry.write_result(result)
    bridge.reserve(root, af3.run_id, record['spec_sha256'])


@pytest.mark.parametrize('mutation', ['unsigned', 'oversize', 'reuse', 'retry', 'badbatch', 'wrongbackend', 'budget'])
def test_rejects_outside_scope(queued, mutation):
    root, run_id = queued
    spec = convert(root, run_id)
    if mutation == 'unsigned':
        spec.provenance.pop('native_approval')
    elif mutation == 'oversize':
        spec.resource_budget.max_candidates = 4
    elif mutation == 'reuse':
        spec.parameters['reuse_output_prefix'] = 'old'
    elif mutation == 'retry':
        spec.attempt = 2
    elif mutation == 'badbatch':
        spec.parameters['dispatch_batch'] = '../../other'
    elif mutation == 'wrongbackend':
        spec.provenance['worker_backend'] = 'external-hpc'
    else:
        spec.resource_budget.max_runtime_minutes = 31
    if mutation != 'unsigned':
        spec = approved(spec)
    ArtifactRegistry(root).write_spec(spec)
    with pytest.raises(ValueError):
        bridge.inspect(root, run_id)


def test_input_tamper_rejected_before_execution(queued):
    root, run_id = queued
    spec = convert(root, run_id)
    (root / 'input.csv').write_text('changed')
    spec.input_sha256 = {'input.csv': '0' * 64}
    ArtifactRegistry(root).write_spec(approved(spec))
    record = bridge.inspect(root, run_id)
    bridge.reserve(root, run_id, record['spec_sha256'])
    with pytest.raises(ValueError, match='Frozen input'):
        bridge.run(root, run_id, record['spec_sha256'])


def test_preworker_scheduler_failure_has_one_same_run_replacement(queued):
    root, run_id = queued
    convert(root, run_id)
    initial = bridge.inspect(root, run_id)
    bridge.reserve(root, run_id, initial['spec_sha256'])
    bridge.fail(root, run_id, initial['spec_sha256'], initial['name'], 'FAILED')
    failed = bridge.inspect(root, run_id)
    assert failed['state'] == 'FAILED'
    assert failed['scheduler_replacement_eligible'] is True
    replaced = bridge.replace_failed_scheduler(
        root, run_id, initial['spec_sha256'], initial['name'], 'FAILED'
    )
    assert replaced['state'] == 'QUEUED'
    assert replaced['name'] == initial['name'] + '-r1'
    assert replaced['scheduler_replacement']['scientific_attempt_unchanged'] is True
    retained = replaced['scheduler_replacement']['original_result']
    assert retained['state'] == 'FAILED'
    assert retained['failure_reason'] == 'external_scheduler_failed'
    with pytest.raises(ValueError):
        bridge.replace_failed_scheduler(
            root, run_id, initial['spec_sha256'], initial['name'], 'FAILED'
        )


@pytest.mark.parametrize('evidence', ['worker', 'artifact', 'started', 'wrong_reason'])
def test_scheduler_replacement_rejects_any_possible_execution(queued, evidence):
    root, run_id = queued
    convert(root, run_id)
    record = bridge.inspect(root, run_id)
    bridge.reserve(root, run_id, record['spec_sha256'])
    bridge.fail(root, run_id, record['spec_sha256'], record['name'], 'FAILED')
    registry = ArtifactRegistry(root)
    result = registry.load_result(run_id)
    job = registry.job_dir(run_id)
    if evidence == 'worker':
        (job / 'external_worker_claim').mkdir()
    elif evidence == 'artifact':
        (job / 'artifacts').mkdir()
    elif evidence == 'started':
        result.started_at = result.finished_at
        registry.write_result(result)
    else:
        result.failure_reason = 'engine_return_code_4'
        registry.write_result(result)
    assert bridge.inspect(root, run_id)['scheduler_replacement_eligible'] is False
    with pytest.raises(ValueError):
        bridge.replace_failed_scheduler(
            root, run_id, record['spec_sha256'], record['name'], 'FAILED'
        )


def test_create_uses_bounded_gpu_or_srun_cpu_without_cli():
    ops = Path(__file__).parents[1] / 'ops'
    sys.path.insert(0, str(ops))
    try:
        import dispatch_ligand_smoke as module
        for engine, (gpu, cpu, mem, minutes) in bridge.STAGES.items():
            record = {'name': 'no-submit', 'run_id': 'test', 'spec_sha256': 'a' * 64,
                'workload': 'job' if gpu else 'hpc', 'minutes': minutes,
                'budget': {'gpu_count': gpu, 'cpu_count': cpu, 'memory_gb': mem}}
            args = module.create_args(record)
            assert args[args.index('--quota') + 1] == f'{gpu},{cpu},{mem}'
            assert 'ligand_external_inspire.py' in args[-1]
            if gpu:
                assert '--no-auto-fault-tolerance' in args
                assert args[args.index('--max-time') + 1] == str(minutes / 60)
            else:
                assert args[-1].startswith('srun ')
    finally:
        sys.path.remove(str(ops))


def recovery_case(root, run_id):
    registry = ArtifactRegistry(root)
    convert(root, run_id)
    carsi = bridge.inspect(root, run_id)
    bridge.reserve(root, run_id, carsi['spec_sha256'])
    result = registry.load_result(run_id)
    result.state = 'COMPLETED'
    registry.write_result(result)
    original = convert(root, run_id, 'alphafold3-mpae', '-af3')
    for name in ('candidate_table', 'source_data_json', 'target_config'):
        filename = name + '.json'
        (root / filename).write_text('{"unchanged_scientific_input":true}')
        original.parameters[name] = filename
        original.input_sha256[filename] = sha256_file(root / filename)
    registry.write_spec(approved(original))
    record = bridge.inspect(root, original.run_id)
    bridge.reserve(root, original.run_id, record['spec_sha256'])
    failed = registry.load_result(original.run_id)
    failed.state = 'FAILED'
    registry.write_result(failed)
    retry = original.model_copy(deep=True)
    retry.run_id += '-recovery'
    retry.request_id += '-retry-2'
    retry.attempt, retry.parent_run_id = 2, original.run_id
    result = failed.model_copy(deep=True)
    result.run_id, result.state = retry.run_id, 'QUEUED'
    registry.create(approved(retry), result)
    return original, retry


def test_explicit_af3_recovery_preserves_failed_claim_and_controls_downstream(queued):
    root, run_id = queued
    original, retry = recovery_case(root, run_id)
    folder = root / 'state/external_inspire_batches/ligand-smoke-test'
    old = (folder / 'alphafold3-mpae.json').read_bytes()
    record = bridge.inspect(root, retry.run_id)
    bridge.reserve(root, retry.run_id, record['spec_sha256'])
    assert (folder / 'alphafold3-mpae.json').read_bytes() == old
    assert (folder / 'alphafold3-mpae.attempt2.json').is_file()
    with pytest.raises(ValueError, match='unreserved'):
        bridge.reserve(root, retry.run_id, record['spec_sha256'])
    downstream = convert(root, run_id, 'schrodinger-covdock-lo', '-covdock')
    check = bridge.inspect(root, downstream.run_id)
    with pytest.raises(ValueError, match='not verified complete'):
        bridge.reserve(root, downstream.run_id, check['spec_sha256'])
    registry = ArtifactRegistry(root)
    result = registry.load_result(retry.run_id)
    result.state = 'COMPLETED'
    registry.write_result(result)
    bridge.reserve(root, downstream.run_id, check['spec_sha256'])
    assert registry.load_result(original.run_id).state == 'FAILED'


@pytest.mark.parametrize('mutation', ['budget', 'parameters', 'input', 'parent_success', 'third_attempt', 'other_parent'])
def test_recovery_rejects_changed_science_parent_and_additional_attempts(queued, mutation):
    root, run_id = queued
    original, retry = recovery_case(root, run_id)
    registry = ArtifactRegistry(root)
    if mutation == 'budget':
        retry.resource_budget.max_runtime_minutes = 60
    elif mutation == 'parameters':
        retry.parameters['num_seeds'] = 2
    elif mutation == 'input':
        (root / retry.parameters['candidate_table']).write_text('changed')
        retry.input_sha256[retry.parameters['candidate_table']] = sha256_file(root / retry.parameters['candidate_table'])
    elif mutation == 'parent_success':
        result = registry.load_result(original.run_id)
        result.state = 'COMPLETED'
        registry.write_result(result)
    elif mutation == 'third_attempt':
        retry.attempt = 3
    else:
        retry.parent_run_id = run_id
    registry.write_spec(approved(retry))
    with pytest.raises(ValueError):
        bridge.inspect(root, retry.run_id)


def covdock_recovery_case(root, run_id):
    registry = ArtifactRegistry(root)
    _, af3 = recovery_case(root, run_id)
    record = bridge.inspect(root, af3.run_id)
    bridge.reserve(root, af3.run_id, record['spec_sha256'])
    result = registry.load_result(af3.run_id)
    result.state = 'COMPLETED'
    registry.write_result(result)
    original = convert(root, run_id, 'schrodinger-covdock-lo', '-covdock')
    original.parameters.update(njobs=3, host='localhost:3', resume=False,
        attachment_residue='A:797', reaction_type_column='covdock_reaction_type')
    for name in ('candidate_table', 'receptor'):
        filename = 'covdock_' + name + '.json'
        (root / filename).write_text('{"unchanged_scientific_input":true}')
        original.parameters[name] = filename
        original.input_sha256[filename] = sha256_file(root / filename)
    registry.write_spec(approved(original))
    record = bridge.inspect(root, original.run_id)
    bridge.reserve(root, original.run_id, record['spec_sha256'])
    failed = registry.load_result(original.run_id)
    failed.state = 'FAILED'
    failed.failure_reason = 'engine_return_code_4'
    registry.write_result(failed)
    retry = original.model_copy(deep=True)
    retry.run_id += '-serial-recovery'
    retry.request_id += '-retry-2'
    retry.attempt, retry.parent_run_id = 2, original.run_id
    retry.parameters.update(njobs=1, host='localhost:1')
    result = failed.model_copy(deep=True)
    result.run_id, result.state = retry.run_id, 'QUEUED'
    registry.create(approved(retry), result)
    return original, retry


def test_covdock_serial_recovery_keeps_original_and_requires_completion(queued):
    root, run_id = queued
    original, retry = covdock_recovery_case(root, run_id)
    folder = root / 'state/external_inspire_batches/ligand-smoke-test'
    old = (folder / 'schrodinger-covdock-lo.json').read_bytes()
    record = bridge.inspect(root, retry.run_id)
    bridge.reserve(root, retry.run_id, record['spec_sha256'])
    assert (folder / 'schrodinger-covdock-lo.json').read_bytes() == old
    assert (folder / 'schrodinger-covdock-lo.attempt2.json').is_file()
    with pytest.raises(ValueError, match='unreserved'):
        bridge.reserve(root, retry.run_id, record['spec_sha256'])
    downstream = convert(root, run_id, 'ligand-first-evaluation', '-evaluation')
    check = bridge.inspect(root, downstream.run_id)
    with pytest.raises(ValueError, match='not verified complete'):
        bridge.reserve(root, downstream.run_id, check['spec_sha256'])
    registry = ArtifactRegistry(root)
    result = registry.load_result(retry.run_id)
    result.state = 'COMPLETED'
    registry.write_result(result)
    bridge.reserve(root, downstream.run_id, check['spec_sha256'])
    assert registry.load_result(original.run_id).state == 'FAILED'


def test_job_manager_compiles_exact_covdock_serial_recovery(queued):
    from covscreen.execution import JobManager

    root, run_id = queued
    original, _ = covdock_recovery_case(root, run_id)
    request = JobManager(root).compile_retry_request(
        original.run_id,
        human_approved=True,
        approval_note='Approved one-time serial technical recovery.',
    )
    assert request.attempt == 2
    assert request.parent_run_id == original.run_id
    assert request.parameters['njobs'] == 1
    assert request.parameters['host'] == 'localhost:1'
    assert request.parameters['resume'] is False
    assert request.parameters['candidate_table'] == original.parameters['candidate_table']
    assert request.parameters['reaction_type_column'] == original.parameters['reaction_type_column']


def test_job_manager_rejects_unverified_covdock_recovery_reason(queued):
    from covscreen.execution import JobManager

    root, run_id = queued
    original, _ = covdock_recovery_case(root, run_id)
    registry = ArtifactRegistry(root)
    result = registry.load_result(original.run_id)
    result.failure_reason = 'quality_gate_failed'
    registry.write_result(result)
    with pytest.raises(ValueError, match='verified parallel'):
        JobManager(root).compile_retry_request(
            original.run_id,
            human_approved=True,
            approval_note='Should not be accepted.',
        )


def _timed_out_serial_recovery(root, run_id):
    """Create the exact attempt-2 terminal evidence used by attempt 3."""
    _, retry = covdock_recovery_case(root, run_id)
    record = bridge.inspect(root, retry.run_id)
    bridge.reserve(root, retry.run_id, record['spec_sha256'])
    bridge.fail(root, retry.run_id, record['spec_sha256'], record['name'], 'TIMED_OUT')
    return retry


def test_job_manager_compiles_exact_three_shard_timeout_recovery(queued):
    from covscreen.execution import JobManager

    root, run_id = queued
    retry = _timed_out_serial_recovery(root, run_id)
    request = JobManager(root).compile_retry_request(
        retry.run_id,
        human_approved=True,
        approval_note='Approved bounded three-shard timeout recovery.',
    )
    assert request.attempt == 3
    assert request.parent_run_id == retry.run_id
    assert request.request_id == retry.request_id + '-retry-3'
    assert request.parameters['dispatch_policy'] == 'ligand-hpc-sharded-v1'
    assert retry.parameters.get('checkpoint_size') is None  # Mirrors the historical attempt-2 contract.
    assert request.parameters['checkpoint_size'] == 1
    assert request.parameters['checkpoint_workers'] == 3
    assert request.parameters['checkpoint_worker_cpus'] == 4
    assert request.parameters['njobs'] == 1
    assert request.parameters['host'] == 'localhost:1'
    assert request.parameters['resume'] is False
    assert request.resource_budget.model_dump() == {
        'max_runtime_minutes': 180,
        'gpu_count': 0,
        'cpu_count': 12,
        'memory_gb': 48.0,
        'max_candidates': 3,
    }


def test_external_hpc_accepts_only_exact_signed_three_shard_timeout_recovery(queued):
    from covscreen import external_hpc
    from covscreen.execution import JobManager

    root, run_id = queued
    retry = _timed_out_serial_recovery(root, run_id)
    request = JobManager(root).compile_retry_request(
        retry.run_id,
        human_approved=True,
        approval_note='Approved bounded three-shard timeout recovery.',
    )
    registry = ArtifactRegistry(root)
    parent = registry.load_spec(retry.run_id)
    child = parent.model_copy(deep=True)
    child.run_id = 'test-covdock-sharded-retry-003'
    child.request_id = request.request_id
    child.attempt = request.attempt
    child.parent_run_id = request.parent_run_id
    child.parameters = request.parameters
    child.resource_budget = request.resource_budget
    child.provenance['worker_backend'] = 'external-hpc'
    child = approved(child)
    parent_result = registry.load_result(retry.run_id)
    registry.create(
        child,
        parent_result.model_copy(update={
            'run_id': child.run_id,
            'state': 'QUEUED',
            'attempt': 3,
            'parent_run_id': retry.run_id,
            'failure_reason': None,
        }),
    )
    record = external_hpc.inspect(root, child.run_id)
    assert record['budget']['cpu_count'] == 12
    assert record['budget']['memory_gb'] == 48
    assert external_hpc.reserve(root, child.run_id, record['spec_sha256'])['name'] == record['name']


@pytest.mark.parametrize(
    'mutation',
    ['workers', 'worker_cpus', 'budget', 'reaction', 'candidate', 'parent_state'],
)
def test_three_shard_timeout_recovery_rejects_changed_scope(queued, mutation):
    from covscreen import external_hpc
    from covscreen.execution import JobManager

    root, run_id = queued
    retry = _timed_out_serial_recovery(root, run_id)
    request = JobManager(root).compile_retry_request(
        retry.run_id,
        human_approved=True,
        approval_note='Approved bounded three-shard timeout recovery.',
    )
    registry = ArtifactRegistry(root)
    parent = registry.load_spec(retry.run_id)
    child = parent.model_copy(deep=True)
    child.run_id = 'test-covdock-sharded-reject-003'
    child.request_id = request.request_id
    child.attempt = 3
    child.parent_run_id = retry.run_id
    child.parameters = request.parameters
    child.resource_budget = request.resource_budget
    child.provenance['worker_backend'] = 'external-hpc'
    if mutation == 'workers':
        child.parameters['checkpoint_workers'] = 2
    elif mutation == 'worker_cpus':
        child.parameters['checkpoint_worker_cpus'] = 5
    elif mutation == 'budget':
        child.resource_budget.cpu_count = 16
    elif mutation == 'reaction':
        child.parameters['reaction_type_column'] = 'changed_chemistry'
    elif mutation == 'candidate':
        name = child.parameters['candidate_table']
        child.input_sha256[name] = '0' * 64
    else:
        result = registry.load_result(retry.run_id)
        result.state = 'FAILED'
        registry.write_result(result)
    child = approved(child)
    parent_result = registry.load_result(retry.run_id)
    registry.create(
        child,
        parent_result.model_copy(update={
            'run_id': child.run_id,
            'state': 'QUEUED',
            'attempt': 3,
            'parent_run_id': retry.run_id,
            'failure_reason': None,
        }),
    )
    with pytest.raises(ValueError):
        external_hpc.inspect(root, child.run_id)


@pytest.mark.parametrize('mutation', ['parallel', 'host', 'resume', 'reaction',
    'input', 'receptor', 'budget', 'unsigned', 'third_attempt', 'same_approval'])
def test_covdock_recovery_rejects_changed_science_or_approval(queued, mutation):
    root, run_id = queued
    original, retry = covdock_recovery_case(root, run_id)
    if mutation == 'parallel':
        retry.parameters['njobs'] = 2
    elif mutation == 'host':
        retry.parameters['host'] = 'localhost:3'
    elif mutation == 'resume':
        retry.parameters['resume'] = True
    elif mutation == 'reaction':
        retry.parameters['reaction_type_column'] = 'other_chemistry'
    elif mutation in {'input', 'receptor'}:
        name = retry.parameters['candidate_table' if mutation == 'input' else 'receptor']
        (root / name).write_text('changed')
        retry.input_sha256[name] = sha256_file(root / name)
    elif mutation == 'budget':
        retry.resource_budget.max_runtime_minutes = 120
    elif mutation == 'third_attempt':
        retry.attempt = 3
    retry = approved(retry)
    if mutation == 'unsigned':
        retry.provenance.pop('native_approval')
    elif mutation == 'same_approval':
        retry.provenance['native_approval'] = ArtifactRegistry(root).load_spec(original.run_id).provenance['native_approval']
    ArtifactRegistry(root).write_spec(retry)
    with pytest.raises(ValueError):
        bridge.inspect(root, retry.run_id)
