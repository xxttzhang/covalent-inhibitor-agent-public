"""Pre-VS source, real native registration and recovery; workers never run.

Deposited 2ITY binds the current study. Output replay uses the separately
identified historical 1M17 fixture and its explicit upstream scope isolation.
Neither fixture is evidence of a new scientific preparation experiment.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from covscreen.execution import JobManager
from covscreen.jobs import sha256_file
from covscreen.ligand_design import write
from covscreen.ligand_vs_input import load
from covscreen.ligand_workflow import DESIGN, navigate, archive
from covscreen.mcp.server import dispatch
from covscreen.receptor_input import load_receptor_preparation_input
from test_vs_input_workflow import study, design, call
from test_receptor_execution_workflow import approval
from test_receptor_input_workflow import configure


@pytest.fixture
def prepared_input(study, monkeypatch):
    root, path = study
    call(study, 'prepare_vs_input')
    state = json.loads(path.read_text())
    case = root, state['workflow_id'], path
    config = root / 'configs/local.inspire.yaml'
    config.parent.mkdir(exist_ok=True)
    write(config, {})
    secret = root / 'test-approval.key'
    secret.write_bytes(b'isolated-fixture-not-production-key' * 2)
    secret.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(secret))
    response = call(study, 'prepare_receptor_input')
    assert response['refinement_input'] is None
    return case


@pytest.fixture
def planned(prepared_input):
    configure(prepared_input)
    root, _, path = prepared_input
    call((root, path), 'check_receptor_runtime')
    return prepared_input


@pytest.fixture
def isolated(planned, monkeypatch):
    launches = []
    popen = subprocess.Popen
    def launch(command, *args, **kwargs):
        if len(command) > 1 and Path(command[1]).name == 'run_job_worker.py':
            launches.append(command)
            process = popen([sys.executable, '-B', '-c', 'pass'], *args, **kwargs)
            process.wait(timeout=10)
            return process
        return popen(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', launch)
    return planned, launches


def test_source_binds_before_any_pose_or_refinement_and_is_archived(prepared_input):
    root, identifier, path = prepared_input
    state = json.loads(path.read_text())
    source = state['vs_receptor']
    ref = source['receptor_input']
    loaded = load_receptor_preparation_input(root, ref['artifact_id'], ref['manifest_sha256'])
    assert loaded['source_kind'] == 'sealed-ligand-vs-receptor-v1'
    parents = loaded['acquisition']['parents']
    assert parents['vs_input_artifact_id'] == state['vs_input']['artifact_id']
    assert 'candidate_artifact_id' not in parents
    assert ref['missing_construct_residue_count'] == 27
    assert not state.get('refinement_input') and not state.get('pose_execution')
    response = navigate(root, 'resume', workflow_id=identifier)
    assert 'check_receptor_runtime' in response['workflow']['tools'][DESIGN]['actions']
    assert not list(root.glob('state/jobs/*/spec.json'))
    archive(state, 'test branch change')
    assert state['vs_receptor'] is None
    assert state['branches'][-1]['vs_receptor'] == source


def test_source_change_removes_unsubmitted_preparation_without_faking_failure(prepared_input):
    root, identifier, path = prepared_input
    state = json.loads(path.read_text())
    ref = state['vs_receptor']['receptor_input']
    (root / ref['directory'] / 'structure.pdb').write_bytes(b'changed fixture')
    response = navigate(root, 'status', workflow_id=identifier)
    assert 'receptor_input' not in response['vs_receptor']
    assert response['vs_receptor']['receptor_input_error']['artifact_id'] == ref['artifact_id']
    assert not list(root.glob('state/jobs/*/spec.json'))


def test_separate_approval_and_lost_reply_recover_one_cpu_task(isolated, monkeypatch):
    case, launches = isolated
    root, identifier, path = case
    with pytest.raises(ValueError, match='approval'):
        call((root, path), 'start_receptor_preparation', human_approved=True)
    submit = JobManager.submit
    def lost_reply(manager, request):
        submit(manager, request)
        raise RuntimeError('isolated reply loss after registration')
    monkeypatch.setattr(JobManager, 'submit', lost_reply)
    with pytest.raises(RuntimeError, match='reply loss'):
        dispatch(root, DESIGN, approval(case))
    state = json.loads(path.read_text())
    response = navigate(root, 'reconcile', workflow_id=identifier, expected_revision=state['revision'])
    run = response['vs_receptor']['receptor_preparation']['run_id']
    assert response['execution']['task_ids'] == [run]
    assert response['execution']['state'] == 'submitted'
    assert response['compute_submitted']
    assert response['pending'] is None and response['refinement_input'] is None
    assert 'spec' not in response['vs_receptor']['receptor_preparation']
    assert 'approved_scope' not in response['vs_receptor']['receptor_preparation']
    assert response['vs_receptor']['receptor_preparation']['plan']['resource_budget']['gpu_count'] == 0
    assert len(launches) == len(list(root.glob('state/jobs/*/spec.json'))) == 1
    manager = JobManager(root)
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'COMPLETED', 0
    manager.registry.write_result(result)
    response = call((root, path), 'receptor_preparation_status')
    assert 'use_prepared_receptor_for_vs' not in response['workflow']['tools'][DESIGN]['actions']
    call((root, path), 'refresh_receptor_preparation')
    with pytest.raises(ValueError, match='verified preparation review'):
        call((root, path), 'use_prepared_receptor_for_vs')
    assert json.loads(path.read_text())['vs_input'] == state['vs_input']
    assert len(launches) == 1


def test_unsubmitted_cpu_plan_cannot_be_replaced_with_user_paths(planned):
    root, _, path = planned
    for action in ('prepare_receptor_input', 'check_receptor_runtime', 'use_prepared_receptor_for_vs'):
        with pytest.raises(ValueError):
            call((root, path), action, receptor_path='/unbound/structure.pdb')
    assert not list(root.glob('state/jobs/*/spec.json'))


@pytest.mark.parametrize('change', [
    {'pending': {'request_id': 'pending'}},
    {'vs_execution': {'run_id': 'submitted', 'job_state': 'COMPLETED'}},
    {'pose_execution': {'run_id': 'uncertain', 'job_state': 'RUNNING'}},
    {'vs_input_error': None},
    {'stage': 'REPORTED'},
    {'refinement_input': {'target': 'already downstream'}},
])
def test_rebinding_rejects_unsafe_or_unnecessary_transitions(change):
    from covscreen.ligand_vs_receptor import refresh_available
    state = {'stage': 'NEEDS_POSE_EVALUATION', 'design_id': 'design',
             'vs_input': {'artifact_id': 'old'}, 'vs_input_error': 'stale',
             'vs_receptor': {'use_for_vs': True, 'receptor_preparation': {
                 'evaluation_verification': 'verified', 'receptor_prepared': True}}}
    assert refresh_available(state)
    assert not refresh_available({**state, **change})


class TestHistoricalPreparedSource:
    # The historical construct has unmapped terminal tags. This existing fixture
    # explicitly isolates that upstream policy; production rejection is retained.
    @pytest.fixture
    def design(self, tmp_path, monkeypatch):
        from test_receptor_evidence_workflow import design as historical_design
        from test_ligand_design import graph_design
        # Existing ring-only software fixture isolates candidate generation;
        # this does not relax production pharmacophore protections.
        return graph_design.__wrapped__(historical_design.__wrapped__(tmp_path, monkeypatch), tmp_path)

    def test_registered_quality_return_and_tamper_rejection(self, isolated, monkeypatch):
        from test_receptor_evidence_workflow import PIPELINE, register_outputs
        from covscreen.receptor_admission import admit_isolated_receptor
        from covscreen.ligand_vs_receptor import project
        case, launches = isolated
        root, identifier, path = case
        old_state = json.loads(path.read_text())
        old_ref = old_state['vs_input']
        from test_vs_input_workflow import assessment
        prior_review = call((root, path), 'review_vs_environment', environment_review=assessment(
            call((root, path), 'read_vs_input')))['vs_environment_review']
        response = dispatch(root, DESIGN, approval(case))
        context = response['vs_receptor']
        run = context['receptor_preparation']['run_id']
        manager = JobManager(root)
        folder = manager.registry.job_dir(run) / 'artifacts/receptor-preparation'
        folder.mkdir(parents=True)
        for name in ('heavy', 'relaxation'):
            shutil.copytree(PIPELINE / name, folder / name)
        for name in ('identity-quality.json', 'geometry-quality.json'):
            shutil.copyfile(PIPELINE / name, folder / name)
        ref = context['receptor_input']
        loaded = load_receptor_preparation_input(root, ref['artifact_id'], ref['manifest_sha256'])
        admit_isolated_receptor(loaded['acquisition_directory'], ref['manifest_sha256'],
            loaded['canonical_sequence'], loaded['taxon'], folder / 'heavy', folder / 'relaxation',
            sha256_file(folder / 'relaxation/receipt.json'), folder / 'identity-quality.json',
            sha256_file(folder / 'identity-quality.json'), folder / 'geometry-quality.json',
            sha256_file(folder / 'geometry-quality.json'), folder / 'prepared')
        spec = manager.registry.load_spec(run)
        write(folder / 'pipeline.json', {
            'schema': 'receptor-preparation-pipeline-v1', 'status': 'prepared_isolated_protein_scope',
            'target_acquisition_sha256': ref['manifest_sha256'], 'target_artifact_id': loaded['target_artifact_id'],
            'input_source_kind': loaded['source_kind'], 'input_source_route': loaded['source_route'],
            'prepared_manifest_sha256': sha256_file(folder / 'prepared/prepared-receptor.json'),
            **{key: spec.parameters[key] for key in ('altlocs', 'heavy_seed', 'max_missing_residues')},
            'artifacts': {str(p.relative_to(folder)): sha256_file(p) for p in folder.rglob('*') if p.is_file()},
            'test_fixture': 'Historical output in isolated registry; no new scientific computation'})
        register_outputs(manager, run, folder)
        call((root, path), 'receptor_preparation_status')
        response = call((root, path), 'report_receptor')
        assert 'use_prepared_receptor_for_vs' in response['workflow']['tools'][DESIGN]['actions']
        assert 'prepare_covdock_input' not in response['workflow']['tools'][DESIGN]['actions']
        assert 'candidate_artifact_id' not in response['vs_receptor']['receptor_preparation']['evaluation']
        assert len(json.dumps(response)) < 30000
        # Reconsider invalidates these histories to null in real persisted state.
        # New-study fixtures previously omitted the keys and missed this path.
        archived_state = json.loads(path.read_text())
        archived_state.update(vs_input_history=None, vs_plan_history=None)
        write(path, archived_state)
        response = call((root, path), 'use_prepared_receptor_for_vs')
        state = json.loads(path.read_text())
        assert state['vs_input'] != old_ref and state['vs_input_history'] == [old_ref]
        assert state['vs_receptor']['source_input'] == old_ref
        assert state['stage'] == old_state['stage']
        assert not state.get('vs_environment_review') and not state.get('vs_plan')
        assert state['vs_plan_history'][-1]['vs_environment_review'] == prior_review
        assert response['refinement_input'] is None and response['result']['md_ready'] is False
        payload = load(root, state)
        assert payload['receptor_prepared'] and not payload['environment_suitability_verified']
        assert payload['review_requirements']['unresolved_residues_rebuilt']
        new_dir, old_dir = root / state['vs_input']['directory'], root / old_ref['directory']
        assert (new_dir / 'receptor-projection.pdb').read_bytes() == (old_dir / 'receptor-projection.pdb').read_bytes()
        original = (folder / 'prepared/prepared-receptor.pdb').read_bytes()
        projected = (new_dir / payload['receptor_file']).read_bytes()
        assert [line[30:54] for line in original.splitlines() if line.startswith(b'ATOM')] == [
            line[30:54] for line in projected.splitlines() if line.startswith(b'ATOM')]
        inventory = json.loads((new_dir / 'receptor-inventory.json').read_text())
        with pytest.raises(ValueError, match='missing construct'):
            project(b'END\n', inventory)
        restored = navigate(root, 'resume', workflow_id=identifier)
        assert restored['vs_input'] == state['vs_input']
        from test_vs_input_workflow import configure as configure_vendor
        configure_vendor(root)  # Inert vendor files; compile only, never execute.
        planned_vs = call((root, path), 'check_vs_runtime')
        assert planned_vs['vs_runtime']['state'] == 'PLAN_COMPILED_ENVIRONMENT_REVIEW_REQUIRED'
        state = json.loads(path.read_text())
        plan = json.loads((root / state['vs_plan']['path']).read_text())
        assert plan['parameters']['vs_preparation']['plan']['receptor'] == str(
            (new_dir / 'prepared-vs-receptor.pdb').relative_to(root))
        assert 'start_vs' not in planned_vs['workflow']['tools'][DESIGN]['actions']
        with pytest.raises(ValueError):
            call((root, path), 'use_prepared_receptor_for_vs')
        assert len(launches) == len(list(root.glob('state/jobs/*/spec.json'))) == 1
        # A changed implementation receipt invalidates the frozen VS input,
        # while the registered receptor coordinates remain unchanged.
        import covscreen.ligand_vs_receptor as vs_receptor
        original_sha = vs_receptor.sha256_file
        monkeypatch.setattr(vs_receptor, 'sha256_file', lambda p:
            'a' * 64 if Path(p) == Path(vs_receptor.__file__) else original_sha(p))
        stale = navigate(root, 'status', workflow_id=identifier)
        assert stale['vs_input_error']
        assert 'use_prepared_receptor_for_vs' in stale['workflow']['tools'][DESIGN]['actions']
        previous_ref = stale['vs_input']
        rebound = call((root, path), 'use_prepared_receptor_for_vs')
        assert rebound['vs_input'] != previous_ref
        assert rebound['result']['previous_vs_input'] == previous_ref
        assert not rebound.get('vs_plan') and not rebound.get('vs_environment_review')
        assert (folder / 'prepared/prepared-receptor.pdb').read_bytes() == original
        assert len(launches) == len(list(root.glob('state/jobs/*/spec.json'))) == 1
        assert not navigate(root, 'status', workflow_id=identifier)['vs_input_error']
        state = json.loads(path.read_text())
        (folder / 'prepared/prepared-receptor.pdb').write_bytes(original + b'\n')
        rejected = navigate(root, 'status', workflow_id=identifier)
        assert rejected['vs_input_error']
        assert not rejected.get('vs_plan')
        assert rejected['vs_receptor']['receptor_preparation']['run_id'] == run
        (folder / 'prepared/prepared-receptor.pdb').write_bytes(original)
        restored = navigate(root, 'resume', workflow_id=identifier)
        assert not restored['vs_input_error']
        assert restored['workflow']['stage'] == old_state['stage']
        assert restored['vs_receptor']['receptor_preparation']['evaluation_verification'] == 'verified'
        assert 'evidence_error' not in restored['vs_receptor']['receptor_preparation']
        # A temporary change to the original acquisition must also recover the
        # same preparation, rather than trapping the branch on its old error.
        source = root / context['receptor_input']['directory'] / 'structure.pdb'
        raw_source = source.read_bytes()
        source.write_bytes(raw_source + b'\n')
        assert navigate(root, 'status', workflow_id=identifier)['vs_input_error']
        source.write_bytes(raw_source)
        restored = navigate(root, 'resume', workflow_id=identifier)
        assert not restored['vs_input_error']
        assert 'evidence_error' not in restored['vs_receptor']['receptor_preparation']
        assert restored['vs_input'] == state['vs_input']
        assert len(launches) == len(list(root.glob('state/jobs/*/spec.json'))) == 1


def test_failed_preparation_can_select_new_policy_without_resubmitting(isolated):
    case, launches = isolated
    root, identifier, path = case
    dispatch(root, DESIGN, approval(case))
    state = json.loads(path.read_text())
    old = state['vs_receptor']['receptor_preparation']
    run = old['run_id']
    with pytest.raises(ValueError):
        call((root, path), 'select_restrained_receptor_policy', reason='Review frozen observed clashes')
    manager = JobManager(root)
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'FAILED', 1
    manager.registry.write_result(result)
    response = call((root, path), 'receptor_preparation_status')
    assert 'reason' in response['workflow']['tools'][DESIGN]['fields']
    call((root, path), 'refresh_receptor_preparation')
    old = json.loads(path.read_text())['vs_receptor']['receptor_preparation']
    response = call((root, path), 'select_restrained_receptor_policy',
        reason='Review frozen observed clashes with an explicitly restrained proposal')
    state = json.loads(path.read_text())
    handoff = state['vs_receptor']
    assert handoff['receptor_preparation_history'][0]['run_id'] == run
    assert handoff['receptor_preparation_history'][0]['approved_scope'] == old['approved_scope']
    assert 'receptor_preparation' not in handoff
    assert handoff['receptor_relaxation_selection']['previous_run_id'] == run
    assert 'check_receptor_runtime' in response['workflow']['tools'][DESIGN]['actions']
    response = call((root, path), 'check_receptor_runtime')
    new = json.loads(path.read_text())['vs_receptor']['receptor_preparation']
    assert new['request_id'] != old['request_id']
    assert new['approval'] == 'NOT_REQUESTED'
    assert new['spec']['parameters']['relaxation_policy_version'] == 'v2'
    assert len(launches) == len(list(root.glob('state/jobs/*/spec.json'))) == 1
    with pytest.raises(ValueError, match='approval'):
        call((root, path), 'start_receptor_preparation', human_approved=True)


def test_motion_review_actions_bind_current_evaluation_without_submission(prepared_input, monkeypatch):
    """Isolate science replay; exercise actual workflow routing and review storage."""
    from covscreen import ligand_receptor_execution as execution
    from covscreen.ligand_receptor_evidence import freeze_review
    from covscreen.receptor_environment_review import MOTION_CONCERNS
    root, identifier, path = prepared_input
    state = json.loads(path.read_text())
    evaluation = {'schema': 'ligand-receptor-review-v2', 'receptor_prepared': False,
        'assessment_scope': 'restrained_coordinate_proposal_identity_geometry_and_motion',
        'result': {'observed_motion_acceptance': 'not_evaluated',
            'quality': {'maximum_displacement_angstrom': 0.2}}}
    witness = freeze_review(root, evaluation)
    state['stage'] = 'RECEPTOR_PREPARATION_REVIEW'
    state['vs_receptor']['receptor_preparation'] = {'run_id': 'synthetic-v2',
        'job_state': 'COMPLETED', 'return_code': 0,
        'spec': {'parameters': {'relaxation_policy_version': 'v2'}},
        'evaluation': evaluation, 'evaluation_artifact': witness,
        'evaluation_verification': 'verified', 'receptor_prepared': False}
    write(path, state)
    from covscreen.ligand_workflow import view
    fields = view(state)['tools'][DESIGN]['fields']
    assert {'motion_review', 'evidence_path', 'evidence_locator', 'evidence_offset'} <= set(fields)
    observed = []
    monkeypatch.setattr(execution, 'observe', lambda *args: observed.append('observe'))
    def simulated_replay(_root, current, _output, **kwargs):
        observed.append('review')
        active = current['vs_receptor']['receptor_preparation']
        active['receptor_prepared'] = not bool(active.get('motion_review'))
    monkeypatch.setattr(execution, 'review', simulated_replay)
    monkeypatch.setattr(execution, 'read_projection', lambda *args: observed.append('read_verified_projection'))
    before = path.read_bytes()
    response = call((root, path), 'read_receptor_motion', evidence_path=witness['path'],
        evidence_locator='/result/quality/maximum_displacement_angstrom')
    assert response['result']['value'] == 0.2
    assert path.read_bytes() == before
    citation = {'path': witness['path'], 'sha256': witness['manifest_sha256'],
        'locator': '/result/quality/maximum_displacement_angstrom',
        'observation': 'Synthetic displacement measurement; scientific suitability remains unresolved.'}
    assessment = {name: {'decision': 'evidence_insufficient',
        'rationale': 'This synthetic test has no independent evidence of binding-site preservation.',
        'evidence': [citation], 'limitations': ['Software fixture only.']} for name in MOTION_CONCERNS}
    call((root, path), 'review_receptor_motion', motion_review=assessment)
    saved = json.loads(path.read_text())['vs_receptor']['receptor_preparation']
    assert saved['motion_review']['decision'] == 'evidence_insufficient'
    assert saved['receptor_prepared'] is False
    before = path.read_bytes()
    call((root, path), 'read_receptor_motion')
    assert path.read_bytes() == before
    assert observed == ['read_verified_projection', 'observe', 'review', 'review', 'read_verified_projection']
    assert not list(root.glob('state/jobs/*/spec.json'))
