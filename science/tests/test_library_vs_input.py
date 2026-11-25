"""Native Library planning from registered fixture products; no vendor job."""
from copy import deepcopy
import json

import pytest
import yaml

from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.docking_policy import POLICY
from covscreen.execution import JobManager
from covscreen.library_vs_input import load as load_input
from covscreen.mcp.server import dispatch
from covscreen.receptor_environment_review import CONCERNS
from covscreen.workflow_store import load
from test_library_workflow import call, state_file
from test_library_vs_prescreen_evidence import registered_prescreen
from test_library_vs_prescreen import grant
from test_receptor_handoff import bound


def assessment(output, decision):
    inventory = output['result']['evidence_inventory']
    path = next(name for name in inventory if name.endswith('/receptor-context.json'))
    return {name: {'decision': decision,
        'rationale': 'Software-only test of source-bound judgments, not a scientific endorsement.',
        'evidence': [{'path': path, 'sha256': inventory[path], 'locator': '/identity/author_residue',
            'observation': 'The original author residue number is retained in the receptor context.'}],
        'limitations': ['Software fixture only; biological suitability is not verified.']} for name in CONCERNS}


def inspect_native_evidence(root, output, action):
    jobs = sorted((root / 'state/jobs').glob('*/spec.json'))
    catalog = call(root, output, RESEARCH, action)
    assert 'evidence_path' in catalog['workflow']['tools'][RESEARCH]['fields']
    entry = next(row for row in catalog['result']['evidence']['documents']
                 if row['path'].endswith('/receptor-context.json'))
    field = call(root, catalog, RESEARCH, action, evidence_path=entry['path'],
                 evidence_locator='/identity/author_residue')
    assert field['result']['evidence']['value'] == json.loads((root / entry['path']).read_text())['identity']['author_residue']
    assert field['result']['evidence']['sha256'] == entry['sha256']
    assert field['result']['evidence']['complete']
    with pytest.raises(ValueError, match='bound inventory'):
        call(root, field, RESEARCH, action, evidence_path='configs/local.inspire.yaml')
    with pytest.raises(ValueError):
        call(root, field, RESEARCH, 'check_lo_runtime' if action == 'read_lo_input' else 'check_vs_runtime',
             evidence_path=entry['path'])
    assert sorted((root / 'state/jobs').glob('*/spec.json')) == jobs
    return field


@pytest.mark.parametrize('execute', [False, True])
@pytest.mark.parametrize('instance', [False, True])
def test_registered_candidates_bind_original_receptor_review_and_private_plan(bound, monkeypatch, execute, instance):
    root, checked, manager, spec, result, manifest, output = registered_prescreen(bound, monkeypatch)
    before_jobs = sorted((root / 'state/jobs').glob('*/spec.json'))
    prepared = call(root, checked, RESEARCH, 'prepare_vs_input')
    prepared = inspect_native_evidence(root, prepared, 'read_vs_input')
    state = load(state_file(root, prepared))
    payload = load_input(root, state)
    assert prepared['execution'] == {'scope': 'library_covdock_vs_planning', 'state': 'not_submitted', 'task_ids': []}
    assert prepared['vs_input']['candidate_count'] == 1
    assert prepared['vs_input']['attachment_residue'] == 'A:773'
    assert payload['source_site_mapping']['canonical_cys'] == 797
    assert payload['prescreen_run_id'] == spec.run_id
    assert payload['candidate_review'] == checked['vs_prescreen']['evaluation_artifact']
    assert not payload['md_ready'] and not payload['environment_suitability_verified']
    assert 'start_vs' not in prepared['workflow']['tools'][RESEARCH]['actions']
    missing = call(root, prepared, RESEARCH, 'check_vs_runtime')
    assert missing['vs_runtime']['state'] == 'WAITING_RUNTIME_CONFIGURATION'
    assert load(state_file(root, missing)).get('vs_plan') is None
    # Planning assets only: no executable actually launches.
    vendor = root / 'vendor'
    (vendor / 'utilities').mkdir(parents=True)
    for name in ('run', 'covalent_docking', 'ligprep', 'utilities/prepwizard', 'test.lic'):
        (vendor / name).write_text('software-only compiler fixture, never executed')
    config = root / 'configs/local.inspire.yaml'
    settings = yaml.safe_load(config.read_text())
    settings['schrodinger'] = {'enabled': True, 'root': str(vendor), 'license_file': str(vendor / 'test.lic')}
    if instance:
        settings.setdefault('runtime', {}).update(library_instance='asgbie2', enable_library_vs_instance=True)
    config.write_text(yaml.safe_dump(settings))
    unreviewed = call(root, missing, RESEARCH, 'check_vs_runtime')
    assert unreviewed['vs_runtime']['state'] == 'PLAN_COMPILED_ENVIRONMENT_REVIEW_REQUIRED'
    previous = load(state_file(root, unreviewed))['vs_plan']
    assert str(vendor) not in json.dumps(unreviewed)
    with pytest.raises(ValueError, match='Invalid environment judgment'):
        call(root, unreviewed, RESEARCH, 'review_vs_environment',
             environment_review=assessment(prepared, 'suitable_for_exploratory_refinement'))
    negative = call(root, unreviewed, RESEARCH, 'review_vs_environment',
        environment_review=assessment(prepared, 'evidence_insufficient'))
    state = load(state_file(root, negative))
    assert state.get('vs_plan') is None and state['vs_plan_history'][-1]['vs_plan'] == previous
    assert negative['vs_environment_review']['decision'] == 'evidence_insufficient'
    reviewed = call(root, negative, RESEARCH, 'review_vs_environment',
        environment_review=assessment(prepared, 'suitable_for_exploratory_vs'))
    compiled = call(root, reviewed, RESEARCH, 'check_vs_runtime')
    assert compiled['workflow']['stage'] == 'VS_PLAN_READY'
    assert compiled['vs_runtime']['state'] == 'VS_PLAN_COMPILED'
    assert not compiled['vs_runtime']['native_execution_available']
    state = load(state_file(root, compiled))
    frozen = state['vs_plan']
    private = root / frozen['path']
    assert private.stat().st_mode & 0o777 == 0o600
    native = json.loads(private.read_text())
    packet = native['parameters']['vs_preparation']
    assert packet['worker_backend'] == ('external-instance' if instance else 'external-hpc')
    assert packet['plan'].get('execution_instance') == ('asgbie2' if instance else None)
    assert packet['candidate_count'] == 1 and packet['plan']['njobs'] == 1
    assert packet['plan']['attachment_residue'] == 'A:773'
    assert packet['plan']['source']['route'] == 'library-first'
    assert packet['plan']['source']['workflow_id'] == state['workflow_id']
    assert native['input_sha256'][state['vs_environment_review']['path']] == state['vs_environment_review']['manifest_sha256']
    repeated = call(root, compiled, RESEARCH, 'check_vs_runtime')
    assert load(state_file(root, repeated))['vs_plan'] == frozen
    repeated = call(root, repeated, RESEARCH, 'prepare_vs_input')
    assert repeated['workflow']['stage'] == 'VS_PLAN_READY'
    assert load(state_file(root, repeated))['vs_plan'] == frozen
    unchanged = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': state['workflow_id']})
    assert unchanged['workflow']['revision'] == repeated['workflow']['revision']
    if execute:
        monkeypatch.setitem(POLICY, 'native_vs_workflow_ready', False)
        with pytest.raises(ValueError, match='migration is not yet complete'):
            grant(root, unchanged, action='start_vs')
        monkeypatch.setitem(POLICY, 'native_vs_workflow_ready', True)
        with pytest.raises(ValueError, match='approval'):
            call(root, unchanged, RESEARCH, 'start_vs')
        original_submit = JobManager.submit
        registered = []
        def lose_response(manager, request):
            value = original_submit(manager, request)
            registered.append(value.run_id)
            raise OSError('Controlled loss after Library VS registration')
        monkeypatch.setattr(JobManager, 'submit', lose_response)
        with pytest.raises(OSError, match='Controlled loss'):
            dispatch(root, RESEARCH, grant(root, unchanged, action='start_vs'))
        pending = load(state_file(root, unchanged))
        assert pending['pending']['action'] == 'start_vs'
        recovered = dispatch(root, ENTRY, {'action': 'reconcile', 'workflow_id': state['workflow_id'],
            'expected_revision': pending['revision']})
        assert recovered['pending'] is None
        assert recovered['vs_execution']['run_id'] == registered[0]
        assert recovered['vs_execution']['job_identity_verified']
        assert recovered['vs_execution']['job_state'] == 'QUEUED'
        assert 'approved_scope_sha256' not in recovered['vs_execution']
        assert not recovered['vs_execution']['md_ready']
        assert 'reconsider' not in recovered['workflow']['tools'][ENTRY]['actions']
        assert 'start_vs' not in recovered['workflow']['tools'][RESEARCH]['actions']
        task = manager.registry.job_dir(registered[0])
        assert not (task / 'external_hpc.json').exists()
        observed = call(root, recovered, RESEARCH, 'vs_status')
        assert observed['result']['run_id'] == registered[0]
        # Upstream source tampering invalidates VS observation, not just prescreen.
        source = output / 'stage2_selection_manifest.json'
        source_bytes = source.read_bytes()
        source.write_bytes(source_bytes + b' ')
        rejected = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': state['workflow_id']})
        assert rejected['execution']['state'] == 'outcome_unknown'
        assert not rejected['vs_execution']['job_identity_verified']
        source.write_bytes(source_bytes)
        restored = dispatch(root, ENTRY, {'action': 'resume', 'workflow_id': state['workflow_id']})
        assert restored['vs_execution']['job_identity_verified']
        assert restored['vs_execution']['run_id'] == registered[0]
        stable = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': state['workflow_id']})
        assert stable['workflow']['revision'] == restored['workflow']['revision']
        assert len(registered) == 1
        assert len(list((root / 'state/jobs').glob('*/spec.json'))) == len(before_jobs) + 1
        launcher = vendor / 'ligprep'
        launcher_bytes = launcher.read_bytes()
        launcher.write_bytes(launcher_bytes + b' changed')
        altered_runtime = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': state['workflow_id']})
        assert altered_runtime['vs_execution']['evidence_error']
        assert altered_runtime['execution']['state'] == 'outcome_unknown'
        launcher.write_bytes(launcher_bytes)
        same_task = dispatch(root, ENTRY, {'action': 'resume', 'workflow_id': state['workflow_id']})
        assert same_task['vs_execution']['run_id'] == registered[0]
        assert same_task['vs_execution']['job_identity_verified']
        return
    # Exact current study binding, not just valid files from some study.
    for key, value in [('workflow_id', 'library-flow-' + 'f' * 24),
                       ('site_selection', {'site': 'C798'}), ('target', {'target': 'BTK', 'taxon': 9606})]:
        altered = deepcopy(state)
        altered[key] = value
        with pytest.raises(ValueError):
            load_input(root, altered)
    # Changed prepared inputs revoke the usable plan on ordinary navigation.
    pdb = root / state['vs_input']['directory'] / 'receptor.pdb'
    original = pdb.read_bytes()
    pdb.write_bytes(original + b' ')
    rejected = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': state['workflow_id']})
    assert rejected['vs_input_error'] and rejected['vs_runtime'] is None
    assert rejected['vs_environment_review'] is None
    assert rejected['workflow']['stage'] == 'VS_INPUT_REVIEW'
    pdb.write_bytes(original)
    restored = dispatch(root, ENTRY, {'action': 'resume', 'workflow_id': state['workflow_id']})
    assert not restored['vs_input_error'] and restored['vs_environment_review'] is None
    assert restored['vs_prescreen']['run_id'] == spec.run_id
    reconsidered = call(root, restored, ENTRY, 'reconsider', return_to='structure',
        reason='Retain the original software branch and review the receptor anew')
    archived = load(state_file(root, reconsidered))
    assert archived['vs_input'] is None
    assert archived['branches'][-1]['vs_input'] == state['vs_input']
    assert sorted((root / 'state/jobs').glob('*/spec.json')) == before_jobs
