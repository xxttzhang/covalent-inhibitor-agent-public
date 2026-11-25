"""Artificial signed ledger fixtures; these tests do not run scientific jobs."""
import fcntl
import hashlib
import json

import pytest
from covscreen.approval_scope import approval_scope, normalize_spec
from covscreen.denovo_dispatch_recovery import cancel, confirmed, validate
from covscreen.execution import JobManager, execute_persisted_job
from covscreen.jobs import ArtifactRegistry, JobSpec, RunResult, utc_now
from covscreen.native_approval import CURRENT, signed, seal_spec
from covscreen_de_novo.independent_review import encoded, folder_for


@pytest.fixture
def queued(tmp_path, monkeypatch):
    key = tmp_path/'approval.key'
    key.write_bytes(b'isolated-software-test-key-123456789')
    key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    registry = ArtifactRegistry(tmp_path)
    run, source = 'test-denovo-recovery-001', 'denovo-review-'+'a'*32
    spec = JobSpec(run_id=run, request_id=source+'-covdock', project_id='fixture-project',
        route='de-novo', engine='schrodinger-covdock-lo', command=['never-execute'],
        working_directory=str(tmp_path), output_directory=str(registry.job_dir(run)/'artifacts'),
        parameters={'workflow_id':'study-aaaaaaaa-aaaa', 'denovo_source_id':source,
                    'denovo_source_manifest_sha256':'a'*64, 'input_row_count':2, 'resume':False},
        provenance={'worker_backend':'external-hpc'}, resource_budget={'gpu_count':0,'cpu_count':1,
            'memory_gb':8,'max_runtime_minutes':30,'max_candidates':2},
        execution_mode='execute', human_approved=True, approval_note='Isolated test only')
    normalized = normalize_spec(spec)
    normalized.update(execution_mode='plan', human_approved=False, approval_note=None)
    folder = folder_for(tmp_path, source); folder.mkdir(parents=True)
    (folder/'plan.json').write_bytes(encoded({'schema':'native-denovo-independent-plan-v1',
        'source_id':source,'source_manifest_sha256':'a'*64,'spec':normalized,
        'spec_sha256':hashlib.sha256(encoded(normalized)).hexdigest()}))
    token = CURRENT.set(signed({'scope':{'kind':'job','spec':normalize_spec(spec)}}))
    try:
        spec.provenance['native_approval'] = seal_spec(spec)
    finally:
        CURRENT.reset(token)
    registry.create(spec, RunResult(run_id=run, state='QUEUED', engine=spec.engine, route=spec.route,
        project_id=spec.project_id, submitted_at=utc_now(), message='queued', stdout_path='stdout', stderr_path='stderr'))
    return registry, run


def authorized(registry, run):
    scope = approval_scope(registry.project_root, 'covalent_cancel_job', {'run_id':run,'human_approved':True})
    return CURRENT.set(signed({'tool':'covalent_cancel_job','scope':scope}))


def test_native_cancellation_is_terminal_only_after_proof_and_preserves_spec(queued):
    registry, run = queued
    old = (registry.job_dir(run)/'spec.json').read_bytes()
    token = authorized(registry, run)
    try:
        assert registry.load_result(run).state == 'QUEUED'
        assert cancel(registry, run).state == 'CANCELLED'
    finally:
        CURRENT.reset(token)
    spec, receipt = confirmed(registry, run)
    assert receipt['compute_started'] is False
    assert (registry.job_dir(run)/'spec.json').read_bytes() == old
    assert execute_persisted_job(registry.project_root, run).state == 'CANCELLED'
    assert not (registry.job_dir(run)/'artifacts').exists()


@pytest.mark.parametrize('evidence', ['external_hpc.json', 'external_worker_claim', 'stdout.log', 'artifacts', 'foreign.txt'])
def test_any_dispatch_or_output_evidence_refuses_without_mutating_result(queued, evidence):
    registry, run = queued
    path = registry.job_dir(run)/evidence
    path.mkdir() if evidence in {'external_worker_claim', 'artifacts'} else path.write_text('{}')
    before = (registry.job_dir(run)/'result.json').read_bytes()
    with pytest.raises(ValueError, match='Reservation, worker claim or output'):
        authorized(registry, run)
    assert (registry.job_dir(run)/'result.json').read_bytes() == before


def test_worker_lock_and_post_approval_reservation_are_rechecked(queued):
    registry, run = queued
    token = authorized(registry, run)
    try:
        with (registry.job_dir(run)/'worker.lock').open('a') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            with pytest.raises(ValueError, match='Worker owns'):
                cancel(registry, run)
        (registry.job_dir(run)/'external_hpc.json').write_text('{}')
        with pytest.raises(ValueError, match='Reservation'):
            cancel(registry, run)
    finally:
        CURRENT.reset(token)
    assert registry.load_result(run).state == 'QUEUED'


def test_cancel_without_native_authority_and_tampered_history_fail(queued):
    registry, run = queued
    with pytest.raises(ValueError):
        cancel(registry, run)
    assert registry.load_result(run).state == 'QUEUED'
    spec = registry.load_spec(run); spec.parameters['input_row_count'] = 3
    registry.write_spec(spec)
    with pytest.raises(ValueError, match='Approved job changed'):
        validate(registry, run)


def test_receipt_and_terminal_state_are_both_required(queued):
    registry, run = queued
    token = authorized(registry, run)
    try:
        cancel(registry, run)
    finally:
        CURRENT.reset(token)
    result = registry.load_result(run); result.finished_at = 'changed'
    registry.write_result(result)
    with pytest.raises(ValueError, match='durable evidence'):
        confirmed(registry, run)


def test_unsupported_dispatch_rejected_before_registration(queued, monkeypatch):
    registry, run = queued
    spec = registry.load_spec(run)
    manager = JobManager(registry.project_root)
    # Isolate dispatch validation from license/source compilation, which this
    # artificial job intentionally cannot perform.
    monkeypatch.setattr(manager, 'build_spec', lambda request: spec)
    monkeypatch.setattr(manager, 'worker_backend', lambda request: 'external-hpc')
    from covscreen.jobs import JobSubmissionRequest
    request = JobSubmissionRequest(request_id=spec.request_id, project_id=spec.project_id,
        route=spec.route, engine=spec.engine, parameters=spec.parameters,
        resource_budget=spec.resource_budget)
    with pytest.raises(ValueError, match='bounded CPU HPC boundary'):
        manager._prepare_submission(request)
    assert len(list(registry.root.iterdir())) == 1


def test_recovery_request_preserves_science_and_requires_confirmed_parent(queued, monkeypatch):
    from covscreen_de_novo import independent_review as adapter
    from covscreen_de_novo.covdock_input import REGISTRY
    registry, run = queued
    spec = registry.load_spec(run)
    identity, digest = spec.parameters['denovo_source_id'], spec.parameters['denovo_source_manifest_sha256']
    root = registry.project_root
    folder = root/'state/de-novo-inputs'/identity
    (folder/'independent').mkdir(parents=True)
    (folder/'independent/protein_A.pdb').write_bytes((REGISTRY/'assets/protein_A.pdb').read_bytes())
    files = {name:'a'*64 for name in ('generation/request.json', 'generation/authorization.json', 'generation/status.json',
        'independent/input_manifest.json', 'independent/candidate_000.sdf', 'independent/protein_A.pdb',
        'control/control_manifest.json', 'control/control_afatinib.sdf', 'control/control_crystal_adduct.sdf',
        'control/control_atom_map.json', 'candidates.csv')}
    source = {'study_id':spec.parameters['workflow_id'], 'project_id':spec.project_id, 'files':files,
        'receptor_sha256':'b'*64, 'input_contract':{'input_count':2,'input_sha256':'c'*64,'identity_sha256':'d'*64}}
    # Source chemistry is outside this ledger test; request construction and
    # receptor derivation, approval, cancellation and lineage checks are real.
    reference = {'root':'/isolated-old-release-fixture','release_id':'f'*64,'manifest_sha256':'e'*64}
    spec.provenance['source_release'] = reference
    def validated_source(*args, validation_release=None):
        return folder, {**source, 'source_validation_runtime_sha256': 'a'*64 if validation_release == reference else 'b'*64}
    monkeypatch.setattr(adapter, 'source', validated_source)
    original = adapter.request_for(root, identity, digest, execution_mode='execute')
    original.parameters.pop('dispatch_policy')
    original.parameters['source_validation_runtime_sha256'] = 'a'*64
    spec.parameters = {**original.parameters, 'input_row_count':2}
    spec.provenance.pop('native_approval')
    token = CURRENT.set(signed({'scope':{'kind':'job','spec':normalize_spec(spec)}}))
    try:
        spec.provenance['native_approval'] = seal_spec(spec)
    finally:
        CURRENT.reset(token)
    registry.write_spec(spec)
    normalized = normalize_spec(spec)
    normalized.update(execution_mode='plan',human_approved=False,approval_note=None)
    path = folder_for(root, identity)/'plan.json'
    path.write_bytes(encoded({'schema':'native-denovo-independent-plan-v1','source_id':identity,
        'source_manifest_sha256':digest,'spec':normalized,'spec_sha256':hashlib.sha256(encoded(normalized)).hexdigest(),
        'submit_arguments':original.model_dump(mode='json')}))
    # Lookup is the shared exact-submission ledger; create its real receipt.
    from covscreen.submission_identity import submit_once
    submit_once(JobManager(root), original, lambda request: registry.load_result(run), planned_run_id=run)
    with pytest.raises(ValueError):
        adapter.request_for(root, identity, digest, recovery=True)
    token = authorized(registry, run)
    try:
        cancel(registry, run)
    finally:
        CURRENT.reset(token)
    before = path.read_bytes()
    recovered = adapter.request_for(root, identity, digest, recovery=True)
    assert (recovered.attempt, recovered.parent_run_id, recovered.request_id) == (2,run,original.request_id+'-retry-2')
    assert recovered.parameters.pop('dispatch_policy') == 'denovo-independent-local-v1'
    assert recovered.parameters == original.parameters
    assert recovered.parameters['source_validation_runtime_sha256'] == 'a'*64
    assert recovered.resource_budget == original.resource_budget
    assert path.read_bytes() == before
    source['input_contract']['input_sha256'] = 'e'*64
    with pytest.raises(ValueError, match='changed the frozen candidate'):
        adapter.request_for(root, identity, digest, recovery=True)


def test_historical_source_validation_requires_full_immutable_release(tmp_path):
    from covscreen.source_release import historical_release
    name, content = 'routes/de-novo/scripts/run_source_validation_registered.py', b'# fixture, never executed\n'
    manifest = {'version':'screening-code-release-v1','files':{name:hashlib.sha256(content).hexdigest()}}
    canonical = json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    root = tmp_path/hashlib.sha256(canonical).hexdigest()
    path=root/name;path.parent.mkdir(parents=True);path.write_bytes(content)
    (root/'manifest.json').write_bytes(canonical+b'\n')
    reference={'root':str(root),'release_id':root.name,'manifest_sha256':hashlib.sha256(canonical+b'\n').hexdigest()}
    assert historical_release(reference)['files']==manifest['files']
    with pytest.raises(ValueError,match='signed parent'):
        historical_release({**reference,'manifest_sha256':'0'*64})
    path.write_bytes(content+b'# changed\n')
    with pytest.raises(ValueError,match='file changed'):
        historical_release(reference)
