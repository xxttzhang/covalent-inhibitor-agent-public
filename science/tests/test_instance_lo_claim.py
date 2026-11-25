"""Real signatures/ledger and atomic claims; inert command, no scientific launch."""
import sys

import pytest

from covscreen.jobs import ArtifactRegistry, JobSpec, RunResult, sha256_file, utc_now
from covscreen.native_approval import CURRENT, signed, seal_spec
from covscreen.approval_scope import normalize_spec
from covscreen.instance_lo_claim import claim, inspect, validate_boundary


@pytest.fixture
def queued(tmp_path, monkeypatch):
    key = tmp_path / 'test-only.key'
    key.write_text('isolated-unit-test-key-not-a-real-credential' * 2)
    key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    source = tmp_path / 'source.csv'
    source.write_text('inert input identity test')
    spec = JobSpec(run_id='signed-lo-test', request_id='request-test', project_id='design-test',
        route='ligand-first', engine='schrodinger-covdock-lo', command=[sys.executable, '-c', 'pass'],
        working_directory=str(tmp_path), output_directory=str(tmp_path/'out'),
        parameters={'workflow_id':'ligand-flow-'+'a'*24, 'execution_instance':'asgbie3',
            'dispatch_policy':'ligand-lo-instance-v1', 'expected_input_count':2, 'input_row_count':2,
            'final_n':2, 'njobs':2, 'host':'localhost:2', 'input_score_source':'core', 'resume':False,
            'environment_eligible_for_exploratory_refinement':True},
        input_sha256={'source.csv':sha256_file(source)},
        provenance={'worker_backend':'external-instance'},
        resource_budget={'max_runtime_minutes':240,'gpu_count':0,'cpu_count':2,'memory_gb':8,'max_candidates':2},
        execution_mode='execute',human_approved=True,approval_note='Isolated inert software test only')
    token = CURRENT.set(signed({'scope':{'kind':'job','spec':normalize_spec(spec)}}))
    try:
        spec.provenance['native_approval'] = seal_spec(spec)
    finally:
        CURRENT.reset(token)
    registry = ArtifactRegistry(tmp_path)
    result = RunResult(run_id=spec.run_id,state='QUEUED',engine=spec.engine,route=spec.route,
        project_id=spec.project_id,submitted_at=utc_now(),message='inert',stdout_path='stdout',stderr_path='stderr')
    registry.create(spec,result)
    return tmp_path,registry,spec


def test_signed_instance_identity_and_atomic_no_replay(queued):
    root,registry,spec=queued
    record=inspect(root,spec.run_id,'asgbie3')
    assert record['engine']=='schrodinger-covdock-lo' and not record['worker_claimed']
    with pytest.raises(ValueError,match='exact queued'):
        claim(root,spec.run_id,'0'*64,'asgbie3')
    claim(root,spec.run_id,record['spec_sha256'],'asgbie3')
    with pytest.raises(FileExistsError):
        claim(root,spec.run_id,record['spec_sha256'],'asgbie3')
    assert registry.load_result(spec.run_id).state=='QUEUED' # claim is not execution


def test_ligand_instance_binding_requires_matching_plan_and_worker(queued, monkeypatch):
    from covscreen.execution import JobManager
    from covscreen.jobs import JobSubmissionRequest
    root, _, original = queued
    monkeypatch.setenv('COVALENT_LIGAND_VS_INSTANCE', 'asgbie4')
    spec = original.model_copy(deep=True)
    with pytest.raises(ValueError, match='bounded native'):
        validate_boundary(spec, 'asgbie3')
    spec.parameters['execution_instance'] = 'asgbie4'
    validate_boundary(spec, 'asgbie4')
    with pytest.raises(ValueError, match='bounded native'):
        validate_boundary(spec, 'asgbie3')
    request = JobSubmissionRequest(request_id='new-instance', project_id='fixture', route='ligand-first',
        engine=spec.engine, parameters=spec.parameters, resource_budget=spec.resource_budget)
    assert JobManager(root).worker_backend(request) == 'external-instance'
    monkeypatch.setenv('COVALENT_LIGAND_VS_INSTANCE', 'asgbie2')
    with pytest.raises(ValueError, match='Unregistered Ligand'):
        validate_boundary(spec, 'asgbie2')


def test_batch_lo_instance_boundary_keeps_ligand_limit_and_requires_enablement(queued):
    from covscreen.execution import JobManager
    from covscreen.jobs import JobSubmissionRequest
    root, _, original = queued
    spec = original.model_copy(deep=True)
    spec.route = 'de-novo'
    spec.parameters.update(workflow_id='de-flow-'+'b'*24, dispatch_policy='denovo-batch-lo-instance-v1',
        denovo_batch_lo_sha256='c'*64, expected_input_count=12, input_row_count=12, final_n=12,
        njobs=12, host='localhost:12')
    spec.resource_budget.max_candidates = 12
    spec.resource_budget.cpu_count = 12
    spec.resource_budget.memory_gb = 48
    validate_boundary(spec)
    request = JobSubmissionRequest(request_id='batch-fixture', project_id='batch-fixture', route='de-novo',
        engine=spec.engine, parameters=spec.parameters, resource_budget=spec.resource_budget)
    manager = JobManager(root)
    assert manager.worker_backend(request) == 'external-instance'
    execute = request.model_copy(update={'execution_mode': 'execute'})
    with pytest.raises(ValueError, match='not enabled'):
        manager.worker_backend(execute)
    manager.config['runtime'] = {'enable_denovo_lo_instance': True}
    assert manager.worker_backend(execute) == 'external-instance'
    spec.route = 'ligand-first'
    with pytest.raises(ValueError, match='bounded native'):
        validate_boundary(spec)


@pytest.mark.parametrize('mutation',['signature','input','instance','budget','terminal'])
def test_reject_changed_or_unapproved_task(queued, mutation):
    root,registry,spec=queued
    if mutation=='signature':
        spec.command.append('changed');registry.write_spec(spec)
    elif mutation=='input':
        (root/'source.csv').write_text('changed')
    elif mutation=='budget':
        changed=spec.model_copy(deep=True);changed.resource_budget.cpu_count=8
        with pytest.raises(ValueError,match='bounded native'):
            validate_boundary(changed)
        return
    elif mutation=='terminal':
        result=registry.load_result(spec.run_id);result.state='COMPLETED';registry.write_result(result)
        record=inspect(root,spec.run_id,'asgbie3')
        with pytest.raises(ValueError,match='exact queued'):
            claim(root,spec.run_id,record['spec_sha256'],'asgbie3')
        return
    with pytest.raises(ValueError):
        inspect(root,spec.run_id,'other-instance' if mutation=='instance' else 'asgbie3')


def test_instance_backend_requires_installation_enablement(queued):
    from covscreen.execution import JobManager
    from covscreen.jobs import JobSubmissionRequest
    root,registry,spec=queued
    manager=JobManager(root)
    request=JobSubmissionRequest(request_id=spec.request_id,project_id=spec.project_id,
        route=spec.route,engine=spec.engine,parameters=spec.parameters,resource_budget=spec.resource_budget)
    assert manager.worker_backend(request)=='external-instance'
    execute=request.model_copy(update={'execution_mode':'execute','human_approved':True})
    with pytest.raises(ValueError,match='not enabled'):
        manager.worker_backend(execute)
    manager.config['runtime']={'enable_ligand_lo_instance':True}
    assert manager.worker_backend(execute)=='external-instance'
    with pytest.raises(ValueError,match='bound Ligand'):
        manager.worker_backend(execute.model_copy(update={'route':'library-first'}))


def test_scientific_validation_isolated_from_supervisor(tmp_path):
    import os
    import subprocess
    import textwrap
    package = tmp_path / 'covscreen'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'instance_claim.py').write_text('''def inspect(root, run_id, instance):
 import threading
 event = threading.Event()
 threading.Thread(target=event.wait, daemon=True).start()
 return {'threads': threading.active_count(), 'run_id': run_id}
''')
    program = textwrap.dedent('''
        import sys, threading
        from pathlib import Path
        from covscreen.instance_claim import inspect_isolated
        from covscreen.instance_process import InstanceProcessTree
        sys.path.insert(0, sys.argv[1])
        before = threading.active_count()
        result = inspect_isolated(Path(sys.argv[1]), 'test-run', 'asgbie3')
        assert result == {'threads': 2, 'run_id': 'test-run'}
        assert threading.active_count() == before == 1
        if sys.platform == 'linux':
            tree = InstanceProcessTree()
            import time
            assert tree.run([sys.executable, '-c', 'pass'], deadline=time.monotonic()+5,
                cancelled=lambda: False) == 0
    ''')
    subprocess.run([sys.executable, '-c', program, str(tmp_path)], check=True,
        env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)}, timeout=20)


@pytest.mark.parametrize('mutation', [None, 'reader', 'running', 'different_failure', 'output', 'signature', 'locked'])
def test_batch_retry_requires_terminated_pre_engine_failure(queued, monkeypatch, mutation):
    import fcntl
    import hashlib
    import json
    from covscreen.layout import enable_de_novo
    enable_de_novo()
    from covscreen_de_novo.batch_lo_retry import parent, FAILURE
    root, registry, spec = queued
    folder = root / 'state/de-novo-lo-inputs' / ('a' * 64)
    folder.mkdir(parents=True)
    (folder / 'input.json').write_text('inert source')
    digest = sha256_file(folder / 'input.json')
    payload = {'workflow_id': 'de-flow-'+'b'*24}
    def source(root, requested):
        if requested == digest: return payload, folder
        revised = root / 'state/de-novo-lo-inputs' / requested
        return json.loads((revised / 'input.json').read_text()), revised
    monkeypatch.setattr('covscreen_de_novo.batch_lo_input.load', source)
    spec.route = 'de-novo'
    spec.project_id = payload['workflow_id']
    spec.request_id = spec.project_id + '-lo-' + digest[:12]
    spec.parameters['denovo_batch_lo_sha256'] = digest
    spec.input_sha256 = {str(folder.relative_to(root) / 'input.json'): digest}
    job = registry.job_dir(spec.run_id)
    spec.output_directory = str(job / 'artifacts')
    spec.provenance.pop('native_approval', None)
    token = CURRENT.set(signed({'scope': {'kind': 'job', 'spec': normalize_spec(spec)}}))
    try:
        spec.provenance['native_approval'] = seal_spec(spec)
    finally:
        CURRENT.reset(token)
    registry.write_spec(spec)
    result = registry.load_result(spec.run_id)
    result.project_id, result.route = spec.project_id, spec.route
    result.state, result.failure_reason, result.finished_at = 'FAILED', FAILURE, utc_now()
    registry.write_result(result)
    for name in ('worker.lock', 'stdout.log', 'stderr.log'):
        (job / name).write_text('')
    identity = [spec.project_id, spec.request_id, 'execute']
    receipts = root / 'state/submissions'
    receipts.mkdir()
    (receipts / (hashlib.sha256(json.dumps(identity).encode()).hexdigest()+'.json')).write_text(
        json.dumps({'identity': identity, 'state': 'REGISTERED', 'run_id': spec.run_id}))
    if mutation == 'running':
        result.state = 'RUNNING'; registry.write_result(result)
    elif mutation == 'different_failure':
        result.failure_reason = 'engine_error'; registry.write_result(result)
    elif mutation == 'output':
        (job / 'stdout.log').write_text('engine output')
    elif mutation == 'signature':
        spec.command.append('modified'); registry.write_spec(spec)
    lock = (job / 'worker.lock').open('a')
    try:
        if mutation == 'locked': fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if mutation == 'reader': fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if mutation not in (None, 'reader'):
            with pytest.raises(ValueError): parent(root, digest)
        else:
            original, unchanged, proof = parent(root, digest)
            assert original == payload and unchanged == folder
            assert proof['run_id'] == spec.run_id and proof['attempt'] == 2
            assert proof['result_sha256'] == sha256_file(job / 'result.json')
            assert registry.load_result(spec.run_id).state == 'FAILED'
            from covscreen_de_novo.batch_lo_retry import freeze, validate
            (folder / 'review.json').write_text('inert review')
            (folder / 'candidates.csv').write_text('inert candidates')
            retry = freeze(root, digest)
            assert retry['manifest_sha256'] != digest
            assert retry['retry_of'] == proof
            assert freeze(root, retry['manifest_sha256']) == retry
            assert (root / retry['directory'] / 'candidates.csv').read_bytes() == (folder / 'candidates.csv').read_bytes()
            with pytest.raises(ValueError, match='original inputs'):
                validate(root, {**payload, 'retry_of': proof, 'unexpected': 'changed'})
            assert registry.load_result(spec.run_id).state == 'FAILED'

    finally:
        lock.close()
