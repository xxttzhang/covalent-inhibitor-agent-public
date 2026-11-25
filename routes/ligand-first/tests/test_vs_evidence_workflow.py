"""Native registry to scientific report using synthetic Maestro products.

No vendor executable, remote worker, model API or scientific scheduler is run.
The random ligand coordinates deliberately do not represent accepted docking.
"""
import gzip
import json

import pytest
from rdkit import Chem, rdBase
from rdkit.Chem import AllChem

from covscreen.covdock_vs import molecules
from covscreen.covdock_vs_output import expected_product, read
from covscreen.covdock_vs_preparation import inspect_prepared
from covscreen.execution import JobManager
from covscreen.jobs import sha256_file
from covscreen.ligand_workflow import DESIGN, navigate
from covscreen.mcp.server import dispatch
from test_vs_execution_workflow import planned, study, design, approval, call


def write_mae(path, values):
    with Chem.MaeWriter(str(path)) as writer:
        for value in values:
            writer.write(value)


@pytest.fixture
def completed(planned):
    root, state_path = planned
    registered = dispatch(root, DESIGN, approval(planned))
    manager = JobManager(root)
    run = registered['vs_execution']['run_id']
    spec = manager.registry.load_spec(run)
    packet = spec.parameters['vs_preparation']
    state = json.loads(state_path.read_text())
    inputs = root/state['vs_input']['directory']
    folder = manager.registry.job_dir(run)/'artifacts'
    prepared, docking = folder/'prepared', folder/'docking'
    prepared.mkdir(parents=True)
    docking.mkdir()
    receptor = Chem.MolFromPDBFile(str(inputs/'receptor-projection.pdb'), removeHs=False)
    assert receptor is not None
    receptor = Chem.AddHs(receptor, addCoords=True, addResidueInfo=True)
    write_mae(prepared/'receptor.mae', [receptor])
    originals = molecules(root/packet['plan']['original_ligands'], False)
    ligands = []
    for original in originals:
        mol = Chem.AddHs(original)
        assert AllChem.EmbedMolecule(mol, randomSeed=17) == 0
        ligands.append(mol)
    write_mae(prepared/'ligands.mae', ligands)

    product, beta, sg = expected_product(originals[0])
    product = Chem.AddHs(product)
    assert AllChem.EmbedMolecule(product, randomSeed=19) == 0
    for index, atom in enumerate(product.GetAtoms()):
        atom.SetMonomerInfo(Chem.AtomPDBResidueInfo(atomName=f'C{index+1:3}',
            residueName='UNK', residueNumber=1, chainId='L'))
        if index < sg:
            atom.SetIntProp('i_cdock_internal_original_atom_number', index+1)
    remove = [a.GetIdx() for a in product.GetAtomWithIdx(sg).GetNeighbors() if a.GetAtomicNum() == 1]+[sg]
    ligand = Chem.RWMol(product)
    for index in sorted(remove, reverse=True):
        ligand.RemoveAtom(index)
    chain, position = packet['plan']['attachment_residue'].split(':')
    def target(atom):
        info = atom.GetPDBResidueInfo()
        return info.GetChainId() == chain and info.GetResidueNumber() == int(position) and info.GetName().strip() == 'SG'
    sulfur = next(a for a in receptor.GetAtoms() if target(a))
    protein = Chem.RWMol(receptor)
    for index in sorted([a.GetIdx() for a in sulfur.GetNeighbors() if a.GetAtomicNum() == 1], reverse=True):
        protein.RemoveAtom(index)
    sulfur_index = next(a.GetIdx() for a in protein.GetAtoms() if target(a))
    complex_mol = Chem.RWMol(Chem.CombineMols(ligand.GetMol(), protein.GetMol()))
    complex_mol.AddBond(beta, ligand.GetNumAtoms()+sulfur_index, Chem.BondType.SINGLE)
    complex_mol = complex_mol.GetMol()
    complex_mol.UpdatePropertyCache(strict=False)
    name = originals[0].GetProp('_Name')
    complex_mol.SetProp('_Name', name)
    complex_mol.SetProp('s_sd_COMPOUND_ID', name)
    complex_mol.SetIntProp('i_sd_INPUT_SDF_INDEX', 0)
    complex_mol.SetDoubleProp('r_i_docking_score', -5.0)
    raw = docking/('vs_'+sha256_file(manager.registry.job_dir(run)/'spec.json')[:20]+'-out.maegz')
    temporary = docking/'synthetic.mae'
    write_mae(temporary, [complex_mol])
    raw.write_bytes(gzip.compress(temporary.read_bytes()))
    temporary.unlink()
    witness = inspect_prepared(root, packet, prepared)
    rows, counts = read(raw, originals, molecules(prepared/'ligands.mae', True),
        molecules(prepared/'receptor.mae', True)[0], packet['plan']['attachment_residue'],
        expected_sha256=sha256_file(raw))
    identity = {'schema':'covdock-vs-output-identity-v1', 'rdkit_version':rdBase.rdkitVersion,
        'raw_sha256':sha256_file(raw), 'candidate_pose_counts':counts,
        'poses':[{key:row[key] for key in ('pose_index','candidate_id','input_index','score',
            'product_identity_verified','prepared_receptor_identity_verified')} for row in rows],
        'scientific_acceptance':False, 'md_ready':False}
    execution = {'schema':'covdock-vs-execution-result-v1', 'run_id':run, 'engine':spec.engine,
        'mode':'enrichment', 'state':'COMPLETED', 'raw_output':{'path':str(raw.relative_to(root)),
        'sha256':sha256_file(raw)}}
    for filename, value in [('prepared-input-witness.json',witness),
            ('vs-output-identity.json',identity),('vs-execution.json',execution)]:
        (folder/filename).write_text(json.dumps(value))
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'COMPLETED', 0
    result.artifacts = [manager.registry.register_file(run, path, 'other')
        for path in folder.rglob('*') if path.is_file()]
    manager.registry.write_result(result)
    return planned, raw


def test_native_evaluation_report_and_changed_output_recovery(completed):
    study, raw = completed
    root, state_path = study
    output = call(study, 'evaluate_vs')
    record = output['vs_execution']
    summary = record['evaluation_summary']
    assert summary['candidate_count'] == 3 and summary['passing_candidate_count'] == 0
    assert [row['pose_count'] for row in summary['candidates']] == [1,0,0]
    assert [row['status'] for row in summary['candidates'][1:]] == ['NOT_EVALUATED']*2
    review = json.loads((root/record['evaluation_artifact']['path']).read_text())
    measured = review['candidates'][0]
    assert not measured['errors'], measured['errors']
    pose = measured['poses'][0]
    assert 'covalent_cs_distance' in pose['assessment']['failure_reasons']
    assert pose['metrics']['reference_frame']['atom_count'] > 200
    assert not record['md_ready'] and not record['scientific_acceptance']
    report = call(study, 'report_vs')['vs_execution']['report']
    text = (root/report['path']).read_text()
    assert 'No output poses' in text and 'MD admission remains pending' in text
    assert call(study, 'vs_status')['vs_execution']['report'] == report
    before = raw.read_bytes()
    raw.write_bytes(before+b'changed')
    with pytest.raises(ValueError, match='registered evidence'):
        call(study, 'vs_status')
    failed = json.loads(state_path.read_text())['vs_execution']
    assert failed['run_id'] == record['run_id'] and failed['output_validation'] == 'EVIDENCE_REJECTED'
    assert 'evaluation_summary' not in failed and 'report' not in failed
    raw.write_bytes(before)
    recovered = call(study, 'evaluate_vs')['vs_execution']
    assert recovered['evaluation_artifact'] == record['evaluation_artifact']
    assert len(list(root.glob('state/jobs/*/spec.json'))) == 1
    workflow = json.loads(state_path.read_text())['workflow_id']
    # Ordinary study reads must not keep displaying stale scientific approval.
    call(study, 'report_vs')
    raw.write_bytes(before+b'changed')
    stale = navigate(root,'status',workflow_id=workflow)
    assert stale['assessment']['state'] == 'evidence_insufficient'
    assert 'evaluation_summary' not in stale['vs_execution'] and 'report' not in stale['vs_execution']
    assert stale['vs_execution']['output_validation'] == 'EVIDENCE_REJECTED'
    raw.write_bytes(before)
    restored = navigate(root,'resume',workflow_id=workflow)
    assert restored['vs_execution']['evaluation_summary'] == summary
    assert restored['vs_execution']['run_id'] == record['run_id']
    revision = restored['workflow']['revision']
    assert navigate(root,'status',workflow_id=workflow)['workflow']['revision'] == revision
    manager = JobManager(root)
    result = manager.registry.load_result(record['run_id'])
    original_project = result.project_id
    result.project_id = 'different-study'
    manager.registry.write_result(result)
    rejected = navigate(root,'status',workflow_id=workflow)['vs_execution']
    assert not rejected['job_identity_verified']
    assert 'evaluation_summary' not in rejected
    result.project_id = original_project
    manager.registry.write_result(result)
    assert navigate(root,'resume',workflow_id=workflow)['vs_execution']['job_identity_verified']
