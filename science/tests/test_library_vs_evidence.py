"""Native Library report from actual synthetic Maestro files, no vendor compute."""
import gzip
import json

import pytest
import yaml
from rdkit import Chem, rdBase
from rdkit.Chem import AllChem

from covscreen.covdock_vs import molecules
from covscreen.covdock_vs_output import expected_product, read
from covscreen.covdock_vs_preparation import inspect_prepared
from covscreen.execution import JobManager
from covscreen.jobs import sha256_file
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.mcp.server import dispatch
from covscreen.workflow_store import load
from test_library_workflow import call, state_file
from test_library_vs_prescreen_evidence import registered_prescreen
from test_library_vs_prescreen import grant
from test_library_vs_input import assessment
from test_receptor_handoff import bound


def write_mae(path, values):
    with Chem.MaeWriter(str(path)) as writer:
        for value in values:
            writer.write(value)


@pytest.fixture
def completed(bound, monkeypatch):
    root, current, *_ = registered_prescreen(bound, monkeypatch,
        additional_smiles=('c1cc(OCCC)cc(NC(=O)C=C)c1',))
    prepared = call(root, current, RESEARCH, 'prepare_vs_input')
    vendor = root / 'vendor'
    (vendor / 'utilities').mkdir(parents=True)
    for name in ('run', 'covalent_docking', 'ligprep', 'utilities/prepwizard', 'test.lic'):
        (vendor / name).write_text('software compiler fixture, never executed')
    config = root / 'configs/local.inspire.yaml'
    settings = yaml.safe_load(config.read_text())
    settings['schrodinger'] = {'enabled': True, 'root': str(vendor), 'license_file': str(vendor / 'test.lic')}
    config.write_text(yaml.safe_dump(settings))
    reviewed = call(root, prepared, RESEARCH, 'review_vs_environment',
        environment_review=assessment(prepared, 'suitable_for_exploratory_vs'))
    planned = call(root, reviewed, RESEARCH, 'check_vs_runtime')
    registered = dispatch(root, RESEARCH, grant(root, planned, action='start_vs'))
    manager = JobManager(root)
    run = registered['vs_execution']['run_id']
    spec = manager.registry.load_spec(run)
    packet = spec.parameters['vs_preparation']
    state = load(state_file(root, registered))
    inputs = root/state['vs_input']['directory']
    folder = manager.registry.job_dir(run)/'artifacts'
    prepared, docking = folder/'prepared', folder/'docking'
    prepared.mkdir(parents=True)
    docking.mkdir()
    receptor = Chem.MolFromPDBFile(str(inputs/'receptor.pdb'), removeHs=False)
    assert receptor is not None
    # Library retains an already hydrogenated prepared receptor. Unlike the
    # Ligand heavy-only projection fixture, it needs no additional AddHs pass.
    assert receptor.GetNumAtoms() > receptor.GetNumHeavyAtoms()
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
    observed = call(root, registered, RESEARCH, 'vs_status')
    return root, observed, raw


def test_native_library_raw_review_report_and_source_recovery(completed):
    root, observed, raw = completed
    checked = call(root, observed, RESEARCH, 'evaluate_vs')
    record = checked['vs_execution']
    summary = record['evaluation_summary']
    assert summary['candidate_count'] == 2
    assert [item['pose_count'] for item in summary['candidates']] == [1, 0]
    assert summary['candidates'][1]['status'] == 'NOT_EVALUATED'
    assert summary['passing_candidate_count'] == 0
    assert 'covalent_cs_distance' in summary['candidates'][0]['aggregate_pose_failure_reasons']
    assert 'no_output_poses' in summary['candidates'][1]['aggregate_missing_reasons']
    assert checked['assessment']['state'] == 'evidence_insufficient'
    value = json.loads((root / record['evaluation_artifact']['path']).read_text())
    assert value['prescreen_run_id'] == checked['vs_prescreen']['run_id']
    assert value['reference_contact_sites']['sites']
    measured = value['candidates'][0]
    assert not measured['errors'], measured['errors']
    pose = measured['poses'][0]
    assert 'covalent_cs_distance' in pose['assessment']['failure_reasons']
    assert pose['metrics']['scaffold_rmsd']['status'] == 'NOT_APPLICABLE'
    assert pose['metrics']['parent_strain_subtraction'] is False
    assert pose['metrics']['reference_frame']['atom_count'] > 200
    assert pose['metrics']['receptor_coordinates'] == 'this_exact_VS_output_pose'
    assert not record['md_ready'] and not record['scientific_acceptance']
    reported = call(root, checked, RESEARCH, 'report_vs')
    report = reported['vs_execution']['report']
    text = (root / report['path']).read_text()
    assert 'No output poses' in text and 'MD admission remains pending' in text
    assert 'unrelated parent strain' in text
    workflow = checked['workflow']['workflow_id']
    fresh = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': workflow})
    assert fresh['workflow']['revision'] == reported['workflow']['revision']
    assert fresh['vs_execution']['report'] == report
    original = raw.read_bytes()
    raw.write_bytes(original + b' changed')
    with pytest.raises(ValueError, match='Navigation unavailable'):
        call(root, fresh, ENTRY, 'reconsider', return_to='structure',
            reason='Cached terminal status must not archive changed raw evidence')
    rejected = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': workflow})
    assert rejected['assessment']['state'] == 'evidence_insufficient'
    assert 'evaluation_summary' not in rejected['vs_execution']
    assert 'report' not in rejected['vs_execution']
    raw.write_bytes(original)
    restored = dispatch(root, ENTRY, {'action': 'resume', 'workflow_id': workflow})
    assert restored['vs_execution']['evaluation_summary'] == summary
    assert restored['vs_execution']['run_id'] == record['run_id']
    assert restored['vs_execution']['evaluation_artifact'] == record['evaluation_artifact']
    assert len(list((root / 'state/jobs').glob('*/spec.json'))) == 3
    report_path = root / report['path']
    report_bytes = report_path.read_bytes()
    call(root, restored, RESEARCH, 'report_vs')
    report_path.write_bytes(report_bytes + b' forged')
    bad_report = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': workflow})
    assert 'report' not in bad_report['vs_execution']
    assert 'evaluation_summary' not in bad_report['vs_execution']
    report_path.write_bytes(report_bytes)
    resumed = dispatch(root, ENTRY, {'action': 'resume', 'workflow_id': workflow})
    assert resumed['vs_execution']['evaluation_summary'] == summary
    final = call(root, resumed, RESEARCH, 'report_vs')
    assert final['vs_execution']['report'] == report
    reconsidered = call(root, final, ENTRY, 'reconsider', return_to='structure',
        reason='Archive the measured negative branch and retain its original task history')
    archived = load(state_file(root, reconsidered))
    assert archived['vs_execution'] is None and archived['vs_input'] is None
    assert archived['branches'][-1]['vs_execution']['run_id'] == record['run_id']
    assert archived['branches'][-1]['vs_execution']['report'] == report
    assert len(list((root / 'state/jobs').glob('*/spec.json'))) == 3
