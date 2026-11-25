"""Real sealed design to independent receptor input; no vendor computation."""
import json

import pytest

from covscreen.ligand_direct_lo import load, SOURCE
from covscreen.ligand_refinement_input import load_refinement_input
from covscreen.ligand_receptor_source import verify_inputs
from covscreen.ligand_workflow import DESIGN, navigate, view
from test_vs_input_workflow import study, design, call


def test_direct_lo_prepares_without_vs_or_old_pose(study):
    root, path = study
    state = json.loads(path.read_text())
    assert 'prepare_direct_lo' in view(state)['tools'][DESIGN]['actions']
    result = call(study, 'prepare_direct_lo')
    assert result['workflow']['stage'] == 'DIRECT_LO_READY'
    state = json.loads(path.read_text())
    ref = state['refinement_input']
    assert ref['source_kind'] == SOURCE
    assert not state.get('vs_input') and not state.get('vs_execution') and not state.get('pose_execution')
    payload = load_refinement_input(root, state)
    assert payload['input_count'] == 3
    assert 'source_run_id' not in payload
    assert payload['routing_decision']['selected_route'] == 'direct_lo'
    table = (root/ref['candidate_table']).read_text()
    assert 'NOT_EVALUATED' in table and 'source_pose_run_id' not in table
    assert call(study, 'prepare_direct_lo')['refinement_input'] == ref
    call(study, 'prepare_direct_lo_target')
    state = json.loads(path.read_text())
    candidates, target = verify_inputs(root, state)
    assert candidates['input_count'] == 3 and target['receptor_prepared'] is False
    actions = navigate(root, 'status', workflow_id=state['workflow_id'])['workflow']['tools'][DESIGN]['actions']
    assert 'prepare_receptor_input' in actions and 'check_af3_runtime' not in actions
    result = call(study, 'prepare_receptor_input')
    assert result['result']['compute_submitted'] is False
    assert not list(root.glob('state/jobs/*/spec.json'))


def test_direct_lo_input_tamper_is_rejected_without_losing_record(study):
    root, path = study
    call(study, 'prepare_direct_lo')
    state = json.loads(path.read_text())
    ref = state['refinement_input']
    (root/ref['candidate_table']).write_text('modified')
    with pytest.raises(ValueError):
        load(root, state)
    output = navigate(root, 'status', workflow_id=state['workflow_id'])
    assert output['refinement_input']['source_error']
    assert output['refinement_input']['artifact_id'] == ref['artifact_id']
    assert not list(root.glob('state/jobs/*/spec.json'))


def test_direct_lo_does_not_replace_existing_vs_branch(study):
    call(study, 'prepare_vs_input')
    with pytest.raises(ValueError, match='idle current design'):
        call(study, 'prepare_direct_lo')


def test_direct_plan_receipts_keep_status_visible_without_repeating_signed_specs(study):
    from copy import deepcopy
    import hashlib
    from covscreen.ligand_workflow import response

    call(study, 'prepare_direct_lo')
    state = json.loads(study[1].read_text())
    spec = {'engine': 'schrodinger-covdock-lo', 'route': 'ligand-first',
            'project_id': state['design_id'], 'request_id': 'receipt-fixture',
            'resource_budget': {'cpu_count': 3, 'max_candidates': 3},
            'parameters': {'execution_instance': 'asgbie3', 'njobs': 3, 'host': 'localhost:3'},
            'input_hashes': {f'input/{i}': 'a' * 64 for i in range(1000)}}
    record = {'run_id': 'receipt-run', 'job_state': 'RUNNING', 'spec': spec,
              'approved_scope': {'recipe': spec}, 'receptor_prepared': False}
    state['refinement_input']['receptor_preparation'] = record
    state['refinement_input']['covdock_plan'] = {'spec': spec, 'runtime_check': 'PLAN_COMPILED'}
    before = deepcopy(state)
    result = response(state, {'receptor_preparation': record,
        'covdock_plan': state['refinement_input']['covdock_plan'], 'run_id': 'receipt-run',
        'job_state': 'RUNNING', 'compute_submitted': False})
    assert state == before
    assert result['result']['run_id'] == 'receipt-run'
    assert result['result']['job_state'] == 'RUNNING'
    assert result['refinement_input']['artifact_id'] == state['refinement_input']['artifact_id']
    for plan in (result['result']['receptor_preparation'], result['result']['covdock_plan'],
                 result['refinement_input']['receptor_preparation'], result['refinement_input']['covdock_plan']):
        assert 'spec' not in plan and 'approved_scope' not in plan
        assert plan['plan']['resource_budget'] == spec['resource_budget']
        assert plan['plan']['execution']['execution_instance'] == 'asgbie3'
        assert plan['plan']['spec_sha256'] == hashlib.sha256(json.dumps(
            spec, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    encoded = json.dumps(result, indent=2)
    assert len(encoded.encode()) < 50 * 1024
    assert len(encoded.splitlines()) < 2000


def test_environment_receipt_preserves_readable_bound_evidence(study):
    from copy import deepcopy
    import hashlib
    from covscreen.ligand_workflow import response
    from covscreen.receptor_environment_review import read_evidence

    call(study, 'prepare_direct_lo')
    root, state_path = study
    state = json.loads(state_path.read_text())
    requirements = {
        'status': 'REVIEW_REQUIRED', 'biological_suitability_verified': False,
        'reactivity_validated': False, 'missing_construct_residue_count': 22,
        'questions': ['Review assembly, exclusions, construct and reaction scope'],
        'construct_audit': {'canonical_positions_absent_from_alignment': list(range(1, 1211))},
        'excluded_environment': {'waters': [{'residue': str(i), 'decision': 'review_required'} for i in range(100)]},
        'site_context': {'observations': [{'residue': str(i), 'distance': i / 10} for i in range(500)]},
    }
    path = 'state/environment-fixture/input.json'
    document = root / path
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(json.dumps({'review_requirements': requirements}))
    digest = hashlib.sha256(document.read_bytes()).hexdigest()
    inventory = {path: digest}
    state['refinement_input']['covdock_input'] = {
        'artifact_id': 'ligand-covdock-input-' + digest[:32], 'path': path, 'manifest_sha256': digest}
    before = deepcopy(state)
    raw = {'review_requirements': requirements, 'evidence_inventory': inventory,
           'evidence': read_evidence(root, inventory), 'execution_authorized': False}
    result = response(state, raw)
    assert state == before and raw['review_requirements'] == requirements
    public = result['result']
    assert public['execution_authorized'] is False
    assert 'evidence_inventory' not in public
    assert public['evidence']['documents'] == [{'path': path, 'sha256': digest}]
    for key in ('construct_audit', 'excluded_environment', 'site_context'):
        reference = public['review_requirements'][key]
        assert reference['path'] in inventory and reference['sha256'] == digest
        page = read_evidence(root, inventory, evidence_path=reference['path'], evidence_locator=reference['locator'])
        assert page['path'] == path and page['sha256'] == digest
        assert page.get('complete') or page.get('children')
    encoded = json.dumps(result, indent=2)
    assert len(encoded.encode()) < 50 * 1024 and len(encoded.splitlines()) < 2000
    document.write_text('{}')
    with pytest.raises(ValueError):
        read_evidence(root, inventory, evidence_path=path)


def test_direct_lo_construct_choices_remain_open_until_target_freezes(study):
    call(study, 'prepare_direct_lo')
    root, path = study
    state = json.loads(path.read_text())
    frozen = state['refinement_input']
    actions = view(state)['tools'][DESIGN]['actions']
    assert {'read_construct_evidence', 'review_construct', 'select_receptor_conformers'} <= set(actions)
    evidence = call(study, 'read_construct_evidence')['result']['construct_evidence']
    conformers = evidence['receptor_conformers']
    selection = {'evidence_sha256': conformers['evidence_sha256'],
        'choices': {row['author_residue_id']: sorted(row['alternatives'])[0] for row in conformers['residues']},
        'rationale': 'Software fixture selection only, not an occupancy ranking.',
        'limitations': 'Does not establish scientific suitability.'}
    call(study, 'select_receptor_conformers', conformer_selection=selection)
    unchanged = path.read_bytes()
    call(study, 'select_receptor_conformers', conformer_selection=selection)
    assert path.read_bytes() == unchanged
    assert load(root, json.loads(path.read_text()))['input_sha256'] == frozen['input_sha256']
    call(study, 'prepare_direct_lo_target')
    state = json.loads(path.read_text())
    assert 'select_receptor_conformers' not in view(state)['tools'][DESIGN]['actions']
    with pytest.raises(ValueError, match='Conformers are frozen'):
        call(study, 'select_receptor_conformers', conformer_selection=selection)
    with pytest.raises(ValueError, match='scope is frozen'):
        call(study, 'review_construct', construct_review={})
    assert not list(root.glob('state/jobs/*/spec.json'))


def test_optional_vs_route_is_explicit_and_archived(study):
    from covscreen.ligand_vs_input import load as load_vs
    from covscreen.ligand_workflow import archive
    root, path = study
    with pytest.raises(ValueError, match='requires a reason'):
        call(study, 'prepare_docking_input', docking_route='vs_then_lo')
    call(study, 'prepare_docking_input', docking_route='vs_then_lo', reason='Small-batch VS comparison fixture')
    state = json.loads(path.read_text())
    payload = load_vs(root, state)
    assert payload['routing_decision']['selected_route'] == 'vs_then_lo'
    assert payload['routing_decision']['candidate_count'] == 3
    before = path.read_bytes()
    with pytest.raises(ValueError, match='without frozen docking inputs'):
        call(study, 'prepare_docking_input')
    assert path.read_bytes() == before
    decision = state['docking_decision']
    archive(state, 'Start an independent branch')
    assert not state.get('docking_decision')
    assert decision in [branch.get('docking_decision') for branch in state['branches']]


def test_direct_receptor_failure_retains_signed_task_and_replans(study, monkeypatch):
    # Real preparation input, native signature, task registry and compiler.
    # Replace only the final worker by an inert child; no receptor repair runs.
    import subprocess
    import sys
    from pathlib import Path
    from covscreen.execution import JobManager
    from covscreen.mcp.server import dispatch
    from test_receptor_execution_workflow import approval
    from test_receptor_input_workflow import configure
    from test_vs_input_workflow import configure as configure_vendor
    root, path = study
    key = root / 'isolated-test-only.key'
    key.write_text('isolated-software-test-not-a-real-credential' * 2)
    key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    call(study, 'prepare_docking_input')
    call(study, 'prepare_direct_lo_target')
    call(study, 'prepare_receptor_input')
    state = json.loads(path.read_text())
    case = root, state['workflow_id'], path
    configure_vendor(root)
    configure(case)
    call(study, 'check_receptor_runtime')
    original, launches = subprocess.Popen, []
    def launch(command, *args, **kwargs):
        if len(command) > 1 and Path(command[1]).name == 'run_job_worker.py':
            launches.append(command)
            child = original([sys.executable, '-B', '-c', 'pass'], *args, **kwargs)
            child.wait(timeout=10)
            return child
        return original(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', launch)
    with pytest.raises(ValueError, match='approval'):
        call(study, 'start_receptor_preparation', human_approved=True)
    response = dispatch(root, DESIGN, approval(case))
    run = response['refinement_input']['receptor_preparation']['run_id']
    registry = JobManager(root).registry
    result = registry.load_result(run)
    result.state, result.return_code = 'FAILED', 1
    registry.write_result(result)
    output = call(study, 'refresh_receptor_preparation')
    assert 'select_local_receptor_policy' in output['workflow']['tools'][DESIGN]['actions']
    output = call(study, 'select_restrained_receptor_policy', reason='Software-only independent proposal following a registered failed preparation')
    assert output['workflow']['stage'] == 'DIRECT_LO_READY'
    output = call(study, 'check_receptor_runtime')
    ref = output['refinement_input']
    assert ref['receptor_preparation']['approval'] == 'NOT_REQUESTED'
    saved = json.loads(path.read_text())['refinement_input']['receptor_preparation']
    assert saved['spec']['parameters']['relaxation_policy_version'] == 'v2'
    assert ref['receptor_preparation_history'][0]['run_id'] == run
    assert len(launches) == len(list(root.glob('state/jobs/*/spec.json'))) == 1
    assert not json.loads(path.read_text()).get('vs_execution')


@pytest.mark.parametrize('count', [6, 7])
def test_full_denominator_routes_before_any_top_n(study, count):
    # Synthetic distinct record identities repeat fixture chemistry only to
    # exercise batch size. This is not evidence of six novel designs or poses.
    from copy import deepcopy
    from rdkit import Chem
    from covscreen.ligand_design import design_folder, seal
    root, path = study
    state = json.loads(path.read_text())
    folder = design_folder(root, state['design_id']) / 'generation'
    original = json.loads((folder / 'candidates.json').read_text())['candidates']
    mols = list(Chem.SDMolSupplier(str(folder / 'candidates.sdf'), removeHs=False))
    rows = []
    with Chem.SDWriter(str(folder / 'candidates.sdf')) as writer:
        for index in range(count):
            row = deepcopy(original[index % len(original)])
            row['candidate_id'] = 'egfr-' + format(index + 1, '016x')
            molecule = Chem.Mol(mols[index % len(mols)])
            molecule.SetProp('candidate_id', row['candidate_id'])
            writer.write(molecule)
            rows.append(row)
    (folder / 'candidates.json').write_text(json.dumps({'candidates': rows}))
    summary = json.loads((folder / 'summary.json').read_text())
    summary['candidate_count'] = count
    (folder / 'summary.json').write_text(json.dumps(summary))
    seal(folder)
    if count == 7:
        with pytest.raises(ValueError, match='vs_then_lo'):
            call(study, 'prepare_direct_lo')
        assert not json.loads(path.read_text()).get('refinement_input')
        call(study, 'prepare_docking_input')
        state = json.loads(path.read_text())
        from covscreen.ligand_vs_input import load as load_vs, request_for
        payload = load_vs(root, state)
        assert payload['selected_candidate_ids'] == [row['candidate_id'] for row in rows]
        assert payload['routing_decision']['candidate_count'] == 7
        assert not payload['candidate_deferrals']
        from test_vs_input_workflow import configure
        configure(root)
        settings = json.loads((root / 'configs/local.inspire.yaml').read_text())
        request = request_for(root, state, settings['schrodinger'])
        assert request.resource_budget.max_candidates == 7
        assert request.parameters['vs_preparation']['plan']['njobs'] == 4
    else:
        call(study, 'prepare_docking_input')
        state = json.loads(path.read_text())
        payload = load(root, state)
        assert payload['input_count'] == 6
        assert payload['selected_candidate_ids'] == [row['candidate_id'] for row in rows]
        assert payload['routing_decision']['candidate_count'] == 6
