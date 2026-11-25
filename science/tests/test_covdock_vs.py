"""Exercise VS identity/byte/budget boundaries using real SDF and Maestro files."""
import copy

import pytest
from pydantic import ValidationError
from rdkit import Chem
from rdkit.Chem import AllChem

from covscreen.covdock_vs import inspect_inputs, revalidate, command, freeze_execution, validate_execution
from covscreen.jobs import sha256_file
from receptor_fixture import write_receptor


def write_ligands(path, molecules):
    writer = Chem.MaeWriter(str(path)) if path.suffix == '.mae' else Chem.SDWriter(str(path))
    with writer:
        for mol in molecules:
            writer.write(mol)


def inputs(tmp_path):
    mol = Chem.AddHs(Chem.MolFromSmiles('NC(=O)[CH:905]=[CH2:906]'))
    assert AllChem.EmbedMolecule(mol, randomSeed=11) == 0
    mol.SetProp('_Name', 'candidate-one')
    write_ligands(tmp_path/'original.sdf', [mol])
    write_ligands(tmp_path/'prepared.mae', [mol])
    write_receptor(tmp_path/'receptor.mae')
    (tmp_path/'source.json').write_text('{"source":"software-only-fixture"}')
    plan = dict(source=dict(route='ligand-first', workflow_id='ligand-flow-'+'a'*24,
        revision=1, manifests={'source.json':sha256_file(tmp_path/'source.json')}),
        original_ligands='original.sdf', prepared_ligands='prepared.mae',
        prepared_receptor='receptor.mae', attachment_residue='A:797',
        budget=dict(cpu_count=4, memory_gb=16, max_runtime_minutes=60, max_candidates=3), njobs=1)
    return plan, mol


def test_vs_witness_and_command_are_distinct_from_lo_and_do_not_grant_approval(tmp_path):
    plan, _ = inputs(tmp_path)
    witness = inspect_inputs(tmp_path, plan)
    assert witness['plan']['engine'] == 'schrodinger-covdock-vs'
    assert witness['plan']['mode'] == 'enrichment'
    assert witness['candidates'][0]['beta_atom_map'] == 906
    assert revalidate(tmp_path, witness) == witness
    assert witness['scientific_acceptance'] is witness['md_ready'] is witness['grants_resource_approval'] is False
    runtime = tmp_path/'vendor'
    runtime.mkdir()
    for name in ('run', 'covalent_docking', 'vendor.lic'):
        (runtime/name).write_text('compile-only test placeholder; never executed')
    argv = command(tmp_path, witness, dict(enabled=True, root=str(runtime), license_file=str(runtime/'vendor.lic')), 'vs_test')
    assert argv[argv.index('-mode')+1] == 'enrichment'
    assert argv[argv.index('-HOST')+1] == 'localhost:1'
    assert argv[argv.index('-RETRIES')+1] == '0'
    assert '-WAIT' in argv
    assert not any('leadopt' in arg or 'carsidock' in arg for arg in argv)


@pytest.mark.parametrize('field,value', [('engine','schrodinger-covdock-lo'), ('mode','leadopt'),
    ('reaction_type','Disulfide Formation'), ('njobs',True)])
def test_no_mode_relabel_or_unvalidated_reaction(tmp_path, field, value):
    plan, _ = inputs(tmp_path)
    plan[field] = value
    with pytest.raises(ValidationError):
        inspect_inputs(tmp_path, plan)


@pytest.mark.parametrize('change', ['mutated_source','mutated_prepared','tampered_witness','symlink','escape'])
def test_frozen_binding_rejects_file_and_record_changes(tmp_path, change):
    plan, _ = inputs(tmp_path)
    witness = inspect_inputs(tmp_path, plan)
    if change == 'mutated_source':
        (tmp_path/'source.json').write_text('{}')
    if change == 'mutated_prepared':
        with (tmp_path/'prepared.mae').open('a') as out:
            out.write('\n')
    if change == 'tampered_witness':
        witness['candidates'][0]['candidate_id'] = 'other'
    if change == 'symlink':
        (tmp_path/'link.mae').symlink_to(tmp_path/'prepared.mae')
        witness['plan']['prepared_ligands'] = 'link.mae'
    if change == 'escape':
        witness['plan']['prepared_ligands'] = '../prepared.mae'
    with pytest.raises(ValueError):
        revalidate(tmp_path, witness)


@pytest.mark.parametrize('change', ['identity','denominator','graph','maps','route','cpu','extra','coordinates'])
def test_prepared_inputs_must_preserve_candidate_and_reaction_identity(tmp_path, change):
    plan, mol = inputs(tmp_path)
    if change == 'identity':
        mol.SetProp('_Name','other')
        write_ligands(tmp_path/'prepared.mae',[mol])
    if change == 'denominator':
        write_ligands(tmp_path/'prepared.mae',[mol,mol])
    if change == 'graph':
        mol.GetAtomWithIdx(0).SetFormalCharge(1)
        write_ligands(tmp_path/'prepared.mae',[mol])
    if change == 'maps':
        for atom in mol.GetAtoms(): atom.SetAtomMapNum(0)
        write_ligands(tmp_path/'original.sdf',[mol])
    if change == 'route':
        plan['source']['route']='library-first'
    if change == 'cpu':
        plan['budget']['cpu_count']=1
        plan['njobs']=2
    if change == 'extra':
        plan['budget']['gpu_count']=1
    if change == 'coordinates':
        mol.GetConformer().SetAtomPosition(0, (float('nan'),0,0))
        write_ligands(tmp_path/'prepared.mae',[mol])
    with pytest.raises((ValueError, RuntimeError)):
        inspect_inputs(tmp_path,plan)


def test_library_uses_its_own_native_lineage_without_an_invented_carsidock_run(tmp_path):
    plan, _ = inputs(tmp_path)
    plan['source'].update(route='library-first', workflow_id='library-flow-'+'b'*24)
    witness = inspect_inputs(tmp_path,plan)
    assert witness['plan']['source']['route'] == 'library-first'
    altered = copy.deepcopy(plan)
    altered['source']['carsidock_run_id']='legacy'
    with pytest.raises(ValidationError): inspect_inputs(tmp_path,altered)


@pytest.mark.parametrize('change', ['executable','license','command','budget','root','approval'])
def test_execution_packet_cannot_change_after_freezing(tmp_path, change):
    plan, _ = inputs(tmp_path)
    witness = inspect_inputs(tmp_path,plan)
    runtime = tmp_path/'vendor'
    runtime.mkdir()
    for name in ('run','covalent_docking','vendor.lic'):
        (runtime/name).write_text('compilation fixture only')
    configuration = dict(enabled=True,root=str(runtime),license_file=str(runtime/'vendor.lic'))
    packet = freeze_execution(tmp_path,witness,configuration,'vs_test')
    assert validate_execution(tmp_path,packet,configuration) == packet
    assert packet['timeout_seconds'] == 3600
    assert packet['native_approval_required'] is True
    if change == 'executable': (runtime/'covalent_docking').write_text('changed installation')
    if change == 'license': (runtime/'vendor.lic').write_text('changed configuration')
    if change == 'command': packet['command'][packet['command'].index('enrichment')] = 'leadopt'
    if change == 'budget': packet['timeout_seconds'] += 60
    if change == 'root': configuration['enabled'] = False
    if change == 'approval': packet['native_approval_required'] = False
    with pytest.raises(ValueError): validate_execution(tmp_path,packet,configuration)


def test_boolean_is_not_a_zero_gpu_budget(tmp_path):
    plan, _ = inputs(tmp_path)
    plan['budget']['gpu_count'] = False
    with pytest.raises(ValidationError): inspect_inputs(tmp_path,plan)
