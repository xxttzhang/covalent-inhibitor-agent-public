"""Compile actual VS plans; never execute placeholder vendor programs."""
import pytest

from covscreen.covdock_vs_preparation import freeze, revalidate, commands, inspect_prepared
from covscreen.covdock_vs_runner import environment, validate_job
from covscreen.execution import JobManager, _engine_environment
from covscreen.jobs import JobSubmissionRequest
from covscreen.external_hpc import validate_boundary
from covscreen.approval_scope import normalize_spec
from test_covdock_vs import inputs, write_ligands
from receptor_fixture import write_receptor, receptor_molecule
from rdkit import Chem
from covscreen.covdock_vs import unresolved_stereo


def test_unspecified_stereochemistry_is_not_a_vendor_selected_isomer():
    ambiguous = Chem.MolFromSmiles('COC(COC)NC(=O)C=C')
    specified = Chem.MolFromSmiles('CO[C@H](COC)NC(=O)C=C')
    assert unresolved_stereo(ambiguous) == [
        {'type': 'Atom_Tetrahedral', 'centered_on': 2, 'specified': 'Unspecified'}]
    assert unresolved_stereo(specified) == []
    assert unresolved_stereo(Chem.MolFromSmiles('CC=CC'))
    assert unresolved_stereo(Chem.MolFromSmiles('C/C=C/C')) == []


def test_plan_rejects_ambiguous_input_before_any_vendor_execution(tmp_path):
    packet, configuration, _ = configured(tmp_path)
    mol = Chem.MolFromSmiles('COC(COC)NC(=O)[CH:905]=[CH2:906]')
    mol.SetProp('_Name', 'unspecified-stereo')
    write_ligands(tmp_path/'original.sdf', [mol])
    with pytest.raises(ValueError, match='explicit stereochemistry before approval'):
        freeze(tmp_path, packet['plan'], configuration)
    assert not list(tmp_path.glob('state/jobs/*/spec.json'))


def configured(tmp_path):
    plan,mol=inputs(tmp_path)
    write_receptor(tmp_path/'raw.pdb')
    vendor=tmp_path/'vendor'
    (vendor/'utilities').mkdir(parents=True)
    for name in ('run','covalent_docking','utilities/prepwizard','ligprep','vendor.lic'):
        (vendor/name).write_text('compile-only fixture; not a vendor runtime')
    configuration=dict(enabled=True,root=str(vendor),license_file=str(vendor/'vendor.lic'))
    values={key:value for key,value in plan.items() if key not in ('prepared_receptor','prepared_ligands')}
    values['receptor']='raw.pdb'
    return freeze(tmp_path,values,configuration),configuration,mol


def request(packet):
    return JobSubmissionRequest(request_id='vs-plan-only',project_id=packet['plan']['source']['workflow_id'],
        route=packet['plan']['source']['route'],engine='schrodinger-covdock-vs',
        parameters={'vs_preparation':packet},resource_budget=packet['plan']['budget'],execution_mode='plan')


def test_registered_compiler_retains_exact_cpu_recipe_and_hpc_boundary(tmp_path):
    packet,configuration,_=configured(tmp_path)
    manager=JobManager(tmp_path)
    manager.config={'schrodinger':configuration}
    spec=manager._prepare_submission(request(packet))
    assert spec.engine=='schrodinger-covdock-vs'
    assert spec.provenance['covdock_mode']=='enrichment'
    assert spec.provenance['worker_backend']=='external-hpc'
    assert spec.provenance['md_ready'] is False
    assert spec.command[1].endswith('/run_covdock_vs.py')
    assert spec.input_sha256['raw.pdb']==packet['input_sha256']['raw.pdb']
    assert spec.input_sha256['original.sdf']==packet['input_sha256']['original.sdf']
    assert not list(tmp_path.rglob('spec.json'))
    validate_boundary(spec.model_copy(update={'execution_mode':'execute','human_approved':True}))


def test_preparation_fixed_recipe_and_identity_readback(tmp_path):
    packet,_,mol=configured(tmp_path)
    argv=commands(tmp_path,packet)
    assert argv[0][2]=='receptor.mae'  # cwd-relative avoids PrepWizard path-name failure
    assert '-fix' in argv[0] and '-noepik' in argv[0]
    assert argv[1][argv[1].index('-i')+1]=='0'
    assert '-nt' in argv[1] and argv[1][argv[1].index('-s')+1]=='1'
    assert all('-LOCAL' in command and '-WAIT' in command for command in argv)
    folder=tmp_path/'prepared';folder.mkdir()
    write_receptor(folder/'receptor.mae')
    write_ligands(folder/'ligands.mae',[mol])
    witness=inspect_prepared(tmp_path,packet,folder)
    assert witness['plan']['prepared_receptor']=='prepared/receptor.mae'
    mol.GetAtomWithIdx(0).SetFormalCharge(1)
    write_ligands(folder/'ligands.mae',[mol])
    with pytest.raises(ValueError,match='changed candidate'): inspect_prepared(tmp_path,packet,folder)


def test_instance_target_is_bound_to_frozen_recipe(tmp_path, monkeypatch):
    hpc, configuration, _ = configured(tmp_path)
    instance = freeze(tmp_path, dict(hpc['plan'], execution_instance='asgbie3'), configuration)
    assert instance['worker_backend'] == 'external-instance'
    assert instance['intent_sha256'] != hpc['intent_sha256']
    assert any(path.endswith('/instance_process.py') for path in instance['sources'])
    assert any(path.endswith('/instance_vs_claim.py') for path in instance['sources'])
    assert revalidate(tmp_path, instance) == instance
    with pytest.raises(ValueError, match='configured runtime changed'):
        revalidate(tmp_path, dict(instance, worker_backend='external-hpc'))
    assert JobManager(tmp_path).worker_backend(request(instance)) == 'external-instance'
    with pytest.raises(ValueError, match='instance VS executor is not enabled'):
        JobManager(tmp_path).worker_backend(request(instance).model_copy(update={'execution_mode': 'execute'}))
    with pytest.raises(ValueError, match='instance VS executor is not enabled'):
        JobManager(tmp_path).worker_backend(request(dict(instance, worker_backend='external-hpc')))
    manager = JobManager(tmp_path)
    manager.config = {'schrodinger': configuration}
    spec = manager._prepare_submission(request(hpc))
    spec = spec.model_copy(update={
        'execution_mode': 'execute', 'human_approved': True,
        'parameters': dict(spec.parameters, vs_preparation=instance),
    })
    approved_recipe = normalize_spec(spec)['parameters']['vs_preparation']
    assert approved_recipe['plan']['execution_instance'] == 'asgbie3'
    assert approved_recipe['plan']['budget'] == instance['plan']['budget']
    # Isolate the worker boundary from signing and release verification.
    monkeypatch.setattr('covscreen.covdock_vs_runner.verify_spec', lambda spec: None)
    monkeypatch.setattr('covscreen.covdock_vs_runner.verify_source', lambda spec: None)
    with pytest.raises(ValueError, match='backend differs from its frozen recipe'):
        validate_job(tmp_path, spec)


def test_library_instance_recipe_preserves_route_and_explicit_activation(tmp_path):
    original, configuration, _ = configured(tmp_path)
    plan = dict(original['plan'], execution_instance='asgbie2')
    plan['source'] = dict(plan['source'], route='library-first', workflow_id='library-flow-' + 'a' * 24)
    packet = freeze(tmp_path, plan, configuration)
    assert packet['worker_backend'] == 'external-instance'
    assert revalidate(tmp_path, packet) == packet
    manager = JobManager(tmp_path)
    intent = request(packet)
    assert manager.worker_backend(intent) == 'external-instance'
    execution = intent.model_copy(update={'execution_mode': 'execute'})
    with pytest.raises(ValueError, match='Library instance VS executor'):
        manager.worker_backend(execution)
    manager.config = {'runtime': {'enable_library_vs_instance': True}}
    assert manager.worker_backend(execution) == 'external-instance'
    with pytest.raises(ValueError, match='route-bound CPU instance'):
        freeze(tmp_path, dict(plan, execution_instance='asgbie3'), configuration)
    with pytest.raises(ValueError, match='route-bound CPU instance'):
        freeze(tmp_path, dict(original['plan'], execution_instance='asgbie2'), configuration)
    altered = dict(packet, plan=dict(packet['plan'], execution_instance='asgbie3'))
    with pytest.raises(ValueError, match='Library instance VS executor'):
        manager.worker_backend(request(altered))


def test_preparation_cannot_substitute_other_protein_atoms_while_retaining_target_sg(tmp_path):
    packet, _, mol = configured(tmp_path)
    folder = tmp_path / 'prepared'; folder.mkdir()
    receptor = receptor_molecule()
    extra = Chem.RWMol(receptor)
    atom = Chem.Atom(6)
    atom.SetMonomerInfo(Chem.AtomPDBResidueInfo(atomName=' CA ', residueName='ALA', residueNumber=798, chainId='A'))
    extra.AddAtom(atom)
    replacement = extra.GetMol()
    replacement.UpdatePropertyCache(strict=False)
    replacement.GetConformer().SetAtomPosition(3, (10., 0., 0.))
    write_receptor(folder / 'receptor.mae', replacement)
    write_ligands(folder / 'ligands.mae', [mol])
    with pytest.raises(ValueError, match='approved receptor heavy atom identity'):
        inspect_prepared(tmp_path, packet, folder)


@pytest.mark.parametrize('change',['input','runtime','recipe','budget','source','mapping'])
def test_preparation_rejects_changed_frozen_facts(tmp_path,change):
    packet,_,mol=configured(tmp_path)
    if change=='input': (tmp_path/'raw.pdb').write_text('invalid PDB')
    if change=='runtime': (tmp_path/'vendor/ligprep').write_text('changed')
    if change=='recipe': packet['preparation_recipe']['ligands']='expand-all'
    if change=='budget': packet['timeout_seconds']+=60
    if change=='source': packet['plan']['source']['revision']+=1
    if change=='mapping':
        for atom in mol.GetAtoms():atom.SetAtomMapNum(0)
        write_ligands(tmp_path/'original.sdf',[mol])
    with pytest.raises((ValueError,RuntimeError)):revalidate(tmp_path,packet)


def test_request_cannot_expand_budget_change_route_or_use_automatic_retry(tmp_path):
    packet,configuration,_=configured(tmp_path)
    manager=JobManager(tmp_path);manager.config={'schrodinger':configuration}
    for field,value in [('route','library-first'),('attempt',2),('project_id','different')]:
        with pytest.raises(ValueError,match='exact source'):manager.build_spec(request(packet).model_copy(update={field:value}))
    expanded=request(packet)
    expanded.resource_budget.cpu_count+=1
    with pytest.raises(ValueError,match='exact source'):manager.build_spec(expanded)


def test_execution_stays_closed_when_native_workflow_switch_is_disabled(tmp_path, monkeypatch):
    from covscreen.docking_policy import POLICY
    monkeypatch.setitem(POLICY, 'native_vs_workflow_ready', False)
    packet,configuration,_=configured(tmp_path)
    manager=JobManager(tmp_path);manager.config={'schrodinger':configuration}
    execute=request(packet).model_copy(update={'execution_mode':'execute','human_approved':True,'approval_note':'Not a native approval'})
    with pytest.raises(ValueError,match='migration is not yet complete'):manager.submit(execute)
    assert not list(tmp_path.rglob('spec.json'))


def test_worker_requires_native_signature_before_preparation(tmp_path):
    packet,configuration,_=configured(tmp_path)
    manager=JobManager(tmp_path);manager.config={'schrodinger':configuration}
    spec=manager._prepare_submission(request(packet))
    with pytest.raises(ValueError):validate_job(tmp_path,spec)
    assert not list(tmp_path.rglob('vs_worker_claim'))


def test_vendor_environment_never_inherits_other_python_or_license_settings(tmp_path):
    packet,configuration,_=configured(tmp_path)
    env=environment(packet,tmp_path/'scratch')
    assert 'PYTHONPATH' not in env and 'PYTHONHOME' not in env
    assert 'LD_PRELOAD' not in env and 'LM_LICENSE_FILE' not in env
    assert env['SCHROD_LICENSE_FILE']==configuration['license_file']
    assert env['OMP_NUM_THREADS']=='1'
    manager=JobManager(tmp_path);manager.config={'schrodinger':configuration}
    spec=manager._prepare_submission(request(packet))
    wrapper=_engine_environment(spec,tmp_path)
    assert wrapper['PYTHONNOUSERSITE']=='1' and 'PYTHONHOME' not in wrapper
