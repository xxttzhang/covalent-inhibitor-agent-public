"""Real frozen 2ITY failure evidence in isolated signed software tasks.

Only copies an existing failed task's artifacts. No repair worker is executed.
"""
import json
import os
from pathlib import Path
import shutil
import pytest
from covscreen.execution import JobManager
from covscreen.jobs import sha256_file
from covscreen.mcp.server import dispatch
from covscreen.ligand_workflow import DESIGN
from test_vs_receptor_workflow import study, design, prepared_input, planned, isolated, call, approval


@pytest.fixture
def failed_local(isolated):
    case, launches = isolated
    root, _, path = case
    response = dispatch(root, DESIGN, approval(case))
    record = response['vs_receptor']['receptor_preparation']
    run = record['run_id']; manager = JobManager(root)
    original = Path(os.environ['COVSCREEN_FAILED_RECEPTOR_FIXTURE_ROOT']) / 'artifacts/receptor-preparation'
    folder = manager.registry.job_dir(run) / 'artifacts/receptor-preparation'
    shutil.copytree(original, folder)
    binding = response['vs_receptor']['receptor_input']
    assert sha256_file(root / binding['directory'] / 'structure.pdb') == json.loads((folder/'heavy/receipt.json').read_text())['source_sha256']
    pipeline = json.loads((folder/'pipeline.json').read_text())
    pipeline['target_acquisition_sha256'] = binding['manifest_sha256']
    pipeline['test_fixture'] = 'Copied real failed 2ITY outputs; new isolated synthetic registry, no repair performed'
    (folder/'pipeline.json').write_text(json.dumps(pipeline))
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'FAILED', 1
    result.artifacts = [manager.registry.register_file(run, f, 'other') for f in sorted(folder.rglob('*')) if f.is_file()]
    manager.registry.write_result(result)
    call((root,path), 'refresh_receptor_preparation')
    return case, launches, folder, run


def test_local_selection_compiles_exact_mask_and_new_budget_without_submission(failed_local):
    case, launches, folder, run = failed_local
    root, _, path = case
    before = sorted(root.glob('state/jobs/*/spec.json'))
    selected = call((root,path), 'select_local_receptor_policy', reason='Review seven source-retained clashes with protected C797 and bounded local motion')
    selection = selected['vs_receptor']['receptor_relaxation_selection']
    assert selection['version'] == 'v2-local'
    assert selection['previous_run_id'] == run
    assert selection['evidence']['movable_observed_atom_count'] == 125
    assert selection['evidence']['observed_atom_count'] == 2396
    mask = selection['policy']['local_observation_mask']
    assert mask['target_entity_position'] == 102
    assert 102 not in mask['movable_entity_positions']
    compiled = call((root,path), 'check_receptor_runtime')
    plan = compiled['vs_receptor']['receptor_preparation']['plan']
    assert plan['relaxation_policy_version'] == 'v2-local'
    assert plan['relaxation_policy_sha256'] == selection['policy_sha256']
    assert plan['resource_budget']['max_runtime_minutes'] == 30
    assert plan['resource_budget']['gpu_count'] == 0
    state = json.loads(path.read_text())
    command = state['vs_receptor']['receptor_preparation']['spec']['command']
    assert command[command.index('--relaxation-policy')+1] == 'v2-local'
    assert json.loads(command[command.index('--relaxation-policy-json')+1]) == selection['policy']
    assert sorted(root.glob('state/jobs/*/spec.json')) == before and len(launches) == 1
    assert state['vs_receptor']['receptor_preparation_history'][0]['run_id'] == run
    # A persisted selection cannot silently broaden the approved proposal.
    state['vs_receptor']['receptor_relaxation_selection']['policy']['local_observation_mask']['atoms'][0]['max_displacement_angstrom'] = 10.
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match='selection differs'):
        call((root,path), 'check_receptor_runtime')
    assert sorted(root.glob('state/jobs/*/spec.json')) == before and len(launches) == 1


def test_changed_registered_collision_evidence_cannot_become_a_local_plan(failed_local):
    case, launches, folder, run = failed_local
    root, _, path = case
    previous = path.read_bytes()
    changed = folder/'geometry-quality.json'
    changed.write_bytes(changed.read_bytes()+b'\n')
    with pytest.raises(ValueError):
        call((root,path), 'select_local_receptor_policy', reason='This changed failure evidence must not become a repair plan')
    assert path.read_bytes() == previous and len(launches) == 1


@pytest.fixture
def failed_harmonic_local(failed_local):
    case, launches, _, original_run = failed_local
    root, _, path = case
    call((root,path),'select_local_receptor_policy',reason='Isolated test: source-bound harmonic local proposal')
    call((root,path),'check_receptor_runtime')
    submitted=dispatch(root,DESIGN,approval(case))
    run=submitted['vs_receptor']['receptor_preparation']['run_id']
    manager=JobManager(root)
    original=Path(os.environ['COVSCREEN_FAILED_LOCAL_RECEPTOR_FIXTURE_ROOT'])/'artifacts/receptor-preparation'
    folder=manager.registry.job_dir(run)/'artifacts/receptor-preparation'
    shutil.copytree(original,folder)
    pipeline=json.loads((folder/'pipeline.json').read_text())
    pipeline['target_acquisition_sha256']=submitted['vs_receptor']['receptor_input']['manifest_sha256']
    # Runtime fixture identity is different; exact spatial policy must be identical.
    assert pipeline['relaxation_policy']==json.loads(path.read_text())['vs_receptor']['receptor_relaxation_selection']['policy']
    pipeline['test_fixture']='Copied failed local trial, isolated signed registration; no minimization performed'
    (folder/'pipeline.json').write_text(json.dumps(pipeline))
    result=manager.registry.load_result(run);result.state,result.return_code='FAILED',1
    result.artifacts=[manager.registry.register_file(run,f,'other') for f in sorted(folder.rglob('*')) if f.is_file()]
    manager.registry.write_result(result)
    call((root,path),'refresh_receptor_preparation')
    return case,launches,folder,original_run,run


def test_bounded_recovery_retains_both_failures_and_requires_new_exact_approval(failed_harmonic_local):
    case,launches,folder,original_run,local_run=failed_harmonic_local
    root,_,path=case
    before=json.loads(path.read_text());jobs=sorted(root.glob('state/jobs/*/spec.json'))
    selected=call((root,path),'select_bounded_local_receptor_policy',reason='Preserve the failed motion trial; propose hard balls with unchanged displacement limits')
    selection=selected['vs_receptor']['receptor_relaxation_selection']
    assert selection['version']=='v2-local-bounded'
    assert selection['previous_run_id']==local_run
    assert selection['evidence']['motion_violation_count']==35
    assert selection['policy']['local_observation_mask']==before['vs_receptor']['receptor_relaxation_selection']['policy']['local_observation_mask']
    call((root,path),'check_receptor_runtime')
    state=json.loads(path.read_text());handoff=state['vs_receptor'];command=handoff['receptor_preparation']['spec']['command']
    assert command[command.index('--relaxation-policy')+1]=='v2-local-bounded'
    assert {x['run_id'] for x in handoff['receptor_preparation_history']}=={original_run,local_run}
    assert handoff['receptor_relaxation_selection_history']==[before['vs_receptor']['receptor_relaxation_selection']]
    assert handoff['receptor_preparation']['approval']=='NOT_REQUESTED'
    assert not handoff['receptor_preparation']['compute_submitted']
    with pytest.raises(ValueError,match='approval'):call((root,path),'start_receptor_preparation',human_approved=True)
    assert sorted(root.glob('state/jobs/*/spec.json'))==jobs and len(launches)==2
    assert handoff['receptor_preparation']['spec']['resource_budget']['max_runtime_minutes']==30


def test_bounded_recovery_rejects_changed_failure_artifact_without_state_write(failed_harmonic_local):
    case,launches,folder,_,_=failed_harmonic_local
    root,_,path=case;before=path.read_bytes()
    f=folder/'identity-quality.json';f.write_bytes(f.read_bytes()+b'\n')
    with pytest.raises(ValueError):call((root,path),'select_bounded_local_receptor_policy',reason='Changed failed evidence cannot authorize a new bounded plan')
    assert path.read_bytes()==before and len(launches)==2


@pytest.fixture
def failed_bounded(failed_harmonic_local):
    case,launches,_,_,_=failed_harmonic_local
    root,_,path=case
    call((root,path),'select_bounded_local_receptor_policy',reason='Isolated software test: bind exact bounded failure diagnostics')
    call((root,path),'check_receptor_runtime')
    submitted=dispatch(root,DESIGN,approval(case));run=submitted['vs_receptor']['receptor_preparation']['run_id']
    manager=JobManager(root);folder=manager.registry.job_dir(run)/'artifacts/receptor-preparation'
    original=Path(os.environ['COVSCREEN_FAILED_BOUNDED_RECEPTOR_FIXTURE_ROOT'])/'artifacts/receptor-preparation'
    shutil.copytree(original,folder)
    pipeline=json.loads((folder/'pipeline.json').read_text())
    pipeline['target_acquisition_sha256']=submitted['vs_receptor']['receptor_input']['manifest_sha256']
    pipeline['test_fixture']='Historical bounded failure; isolated registry, worker replaced with no-op'
    (folder/'pipeline.json').write_text(json.dumps(pipeline))
    result=manager.registry.load_result(run);result.state,result.return_code='FAILED',1
    result.artifacts=[manager.registry.register_file(run,f,'other') for f in sorted(folder.rglob('*')) if f.is_file()]
    manager.registry.write_result(result)
    return case,launches,folder,run


def test_bounded_iteration_failure_is_readable_without_mutation_or_retry(failed_bounded):
    case,launches,folder,run=failed_bounded
    root,_,path=case
    # Repeated observation must preserve the entire workflow and registry bytes.
    tracked=[path,*root.glob('state/jobs/*/spec.json'),*root.glob('state/jobs/*/result.json')]
    before={str(f):f.read_bytes() for f in tracked}
    one=call((root,path),'receptor_preparation_status');two=call((root,path),'receptor_preparation_status')
    assert one==two and before=={str(f):f.read_bytes() for f in tracked}
    diagnostics=one['result']['failure_diagnostics']
    assert diagnostics['failure_stage']=='relaxation'
    assert diagnostics['error']=='Bounded receptor optimizer did not converge: iteration_limit'
    assert 'CalledProcessError' in diagnostics['pipeline_error']
    detail=diagnostics['relaxation']['bounded_optimizer']
    assert detail['status']=='iteration_limit' and detail['iterations']==1000 and detail['evaluations']==2180
    assert detail['required_tolerance_kj_mol_nm']==10
    assert detail['projected_gradient_max_kj_mol_nm']==pytest.approx(24.030590888572007)
    assert not diagnostics['relaxation']['coordinate_identity_available']
    assert not one['result']['receptor_prepared'] and len(launches)==3
    assert any(x['path'].endswith('/relaxation/receipt.json') for x in diagnostics['sources'])
    receipt=folder/'relaxation/receipt.json';receipt.write_bytes(receipt.read_bytes()+b'\n')
    with pytest.raises(ValueError, match='artifact differs from registered evidence'):
        call((root,path),'receptor_preparation_status')
    assert path.read_bytes()==before[str(path)] and len(launches)==3
    assert before=={str(f):f.read_bytes() for f in tracked}


def test_block_recovery_revalidates_history_and_compiles_without_execution(failed_bounded):
    from covscreen.ligand_receptor_preparation import request_for
    case,launches,folder,run=failed_bounded
    root,_,path=case
    call((root,path),'refresh_receptor_preparation')
    before=json.loads(path.read_text());registry={str(f):f.read_bytes() for f in root.glob('state/jobs/*/*.json')}
    old=before['vs_receptor']['receptor_relaxation_selection']['policy']
    response=call((root,path),'select_block_local_receptor_policy',reason='Test explicit block recovery without any molecular execution')
    selection=response['vs_receptor']['receptor_relaxation_selection']
    assert selection['version']=='v2-local-block'
    assert selection['previous_run_id']==run
    assert selection['policy']['local_observation_mask']==old['local_observation_mask']
    assert len(response['vs_receptor']['receptor_preparation_history'])==3
    assert len(response['vs_receptor']['receptor_relaxation_selection_history'])==2
    planned=call((root,path),'check_receptor_runtime')
    spec=planned['vs_receptor']['receptor_preparation']['plan']
    assert spec['relaxation_policy_version']=='v2-local-block'
    state=json.loads(path.read_text());request=request_for(root,state)
    assert request.resource_budget.max_runtime_minutes==30
    assert len(launches)==3 and registry=={str(f):f.read_bytes() for f in root.glob('state/jobs/*/*.json')}
    saved=path.read_bytes()
    receipt=folder/'relaxation/receipt.json';receipt.write_bytes(receipt.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='registered evidence'):
        request_for(root,state)
    assert path.read_bytes()==saved and len(launches)==3
