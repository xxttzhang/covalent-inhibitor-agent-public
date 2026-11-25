"""Known-coordinate adversarial controls, not real redocking or scientific acceptance."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from rdkit import Chem

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'evaluation'))
from control import prepare_control
from covscreen_de_novo import control_review as review

REGISTRY = Path(__file__).resolve().parents[1]/'registry/egfr_c797_v1'
PRECURSOR_SHA256 = 'a063c061aeae94d6d9f99a3d959455ccb3a595ccd9559260605ce67449b74fb1'


@pytest.fixture
def crystal(tmp_path):
    folder = tmp_path/'control'
    prepare_control(REGISTRY/'assets', REGISTRY/'assets/0WM.cif', PRECURSOR_SHA256, folder)
    mol = Chem.MolFromMolFile(str(folder/'control_crystal_adduct.sdf'), removeHs=True)
    names = json.loads((folder/'control_atom_map.json').read_text())
    inverse = {index:name for name,index in names.items()}
    ca = review.protein_reference(REGISTRY/'assets/4G5J.pdb')
    pose = {'pose_index': 0, 'title': 'control_afatinib__p0__i1', 'protein_ca': copy.deepcopy(ca),
        'ligand_heavy_atoms': [{'atom_index': atom.GetIdx()+1, 'atom_name': inverse[atom.GetIdx()],
            'atomic_number': atom.GetAtomicNum(), 'element': atom.GetSymbol(),
            'formal_charge': atom.GetFormalCharge(), 'isotope': atom.GetIsotope(),
            'xyz': list(mol.GetConformer().GetAtomPosition(atom.GetIdx()))} for atom in mol.GetAtoms()],
        'ligand_heavy_bonds': [{'atom_name_1':inverse[b.GetBeginAtomIdx()],
            'atom_name_2':inverse[b.GetEndAtomIdx()], 'order':b.GetBondTypeAsDouble()} for b in mol.GetBonds()],
        'covalent_partner_atom_name':'C30'}
    return mol, ca, names['C30'], pose


def test_rigid_complex_motion_recovers_crystal_without_ligand_fitting(crystal):
    mol, ca, partner, pose = crystal
    rotation = np.array([[0., 1., 0.], [-1., 0., 0.], [0., 0., 1.]])
    for atom in pose['protein_ca']+pose['ligand_heavy_atoms']:
        atom['xyz'] = (np.array(atom['xyz']) @ rotation + [7, 9, -3]).tolist()
    measured = review.measure(mol, ca, partner, pose)
    assert measured['state'] == 'PASS'
    assert measured['heavy_atom_rmsd_A'] < 1e-10
    assert measured['reference_ca_count'] > 200
    assert measured['ligand_fitting'] is False and measured['md_ready'] is False


def test_ligand_only_translation_is_not_fitted_away(crystal):
    mol, ca, partner, pose = crystal
    for atom in pose['ligand_heavy_atoms']:
        atom['xyz'][0] += 3
    measured = review.measure(mol, ca, partner, pose)
    assert measured['state'] == 'FAIL'
    assert measured['heavy_atom_rmsd_A'] == pytest.approx(3)


def test_atom_order_and_symmetric_permutation_are_resolved(crystal):
    mol, ca, partner, pose = crystal
    matches = mol.GetSubstructMatches(mol, uniquify=False, useChirality=True)
    permutation = next(match for match in matches if tuple(range(mol.GetNumAtoms())) != match and match[partner] == partner)
    coords = [row['xyz'] for row in pose['ligand_heavy_atoms']]
    for index, mapped in enumerate(permutation):
        pose['ligand_heavy_atoms'][index]['xyz'] = coords[mapped]
    pose['ligand_heavy_atoms'].reverse()
    measured = review.measure(mol, ca, partner, pose)
    assert measured['heavy_atom_rmsd_A'] < 1e-10
    assert measured['symmetry_mappings_evaluated'] > 1


def test_symmetry_cap_cannot_silently_truncate(crystal):
    with pytest.raises(ValueError, match='enumeration exceeds'):
        review.measure(*crystal, max_mappings=1)


@pytest.mark.parametrize('change,match', [
    ('missing', 'lacks reference'), ('duplicate', 'Ambiguous receptor'),
    ('mutation', 'sequence identity'), ('degenerate', 'Degenerate'), ('nan', 'Invalid receptor')])
def test_receptor_identity_and_coordinates_fail_closed(crystal, change, match):
    mol, ca, partner, pose = crystal
    rows = pose['protein_ca']
    if change == 'missing': rows.pop()
    if change == 'duplicate': rows.append(copy.deepcopy(rows[0]))
    if change == 'mutation': rows[0]['resname'] = 'XXX'
    if change == 'degenerate':
        for index, row in enumerate(rows): row['xyz'] = [index, 0, 0]
    if change == 'nan': rows[0]['xyz'][0] = float('nan')
    with pytest.raises(ValueError, match=match): review.measure(mol, ca, partner, pose)


def test_wrong_attachment_and_graph_not_accepted(crystal):
    mol, ca, partner, pose = crystal
    pose['covalent_partner_atom_name'] = 'C28'
    with pytest.raises(ValueError, match='attachment atom'): review.measure(mol, ca, partner, pose)
    pose['covalent_partner_atom_name'] = 'C30'
    pose['ligand_heavy_atoms'][0]['isotope'] = 13
    with pytest.raises(ValueError, match='graph or stereochemistry'): review.measure(mol, ca, partner, pose)


def test_mirrored_ligand_stereochemistry_is_rejected(crystal):
    mol, ca, partner, pose = crystal
    for atom in pose['ligand_heavy_atoms']: atom['xyz'][0] *= -1
    with pytest.raises(ValueError, match='stereochemistry'): review.measure(mol, ca, partner, pose)


def test_best_score_is_selected_before_rmsd_and_ties_are_deterministic():
    rows = [dict(pose_index=0, title='candidate_000__p0__i0', score=-100),
            dict(pose_index=1, title='control_afatinib__p1__i1', score=-10, hypothetical_rmsd=8),
            dict(pose_index=2, title='control_afatinib__p0__i1', score=-9, hypothetical_rmsd=0)]
    assert review.select_pose(rows, 'score')['pose_index'] == 1
    rows[2]['score'] = -10
    assert review.select_pose(rows, 'score')['pose_index'] == 1
    rows[1]['score'] = None
    with pytest.raises(ValueError, match='finite vendor score'): review.select_pose(rows, 'score')


@pytest.mark.parametrize('rows', [
    [{'pose_index':1,'title':'control_afatinib__p0__i1','score':-1}],
    [{'pose_index':0,'title':'control_afatinib__p0__i0','score':-1}],
    [{'pose_index':0,'title':'candidate_000__p0__i0','score':-1}]])
def test_missing_or_ambiguous_control_census_rejected(rows):
    with pytest.raises(ValueError): review.select_pose(rows, 'score')


def test_frozen_implementation_detects_changed_policy_and_reader():
    frozen = review.freeze()
    review.validate(frozen)
    frozen['policy']['max_heavy_atom_rmsd_A'] = 3
    with pytest.raises(ValueError, match='changed after plan freeze'): review.validate(frozen)
    frozen = review.freeze()
    frozen['implementation']['files'][next(iter(frozen['implementation']['files']))] = 'a'*64
    with pytest.raises(ValueError, match='changed after plan freeze'): review.validate(frozen)


def test_vendor_reader_rejects_unbounded_request_before_importing_sdk():
    scripts = Path(__file__).resolve().parents[3]/'science/scripts'
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location('test_control_reader', scripts/'read_covdock_control.py')
    reader = importlib.util.module_from_spec(spec); spec.loader.exec_module(reader)
    with pytest.raises(ValueError, match='Invalid control readback'): reader.read_control({})


@pytest.fixture
def recorded_control(tmp_path, crystal, monkeypatch):
    """Real files and RMSD; artificial ledger and vendor transport, never a docking result."""
    import pandas as pd
    from covscreen import md_preparation_evidence
    from covscreen.jobs import sha256_file
    folder = tmp_path/'job/artifacts'; folder.mkdir(parents=True)
    pose = crystal[3]
    score = 'r_cdock_cdock_affinity'
    selected = {'pose_index':0, 'title':pose['title'], score:-10}
    (folder/'inspection.json').write_text(json.dumps({'rows':[selected]}))
    (folder/'container.mae').write_text('ARTIFICIAL SDK TRANSPORT FIXTURE; not a structure')
    (folder/'covdock_lo_registered_manifest.json').write_text(json.dumps({'score_property':score}))
    pd.DataFrame([{'input_sdf_index':1, 'compound_id':'control_afatinib', 'covdock_inspection_file':'inspection.json',
        'covdock_inspection_sha256':sha256_file(folder/'inspection.json'),
        'covdock_pose_file':'container.mae','covdock_pose_index':0,'covdock_title':pose['title']}]
        ).to_parquet(folder/'covdock_lo_all_scores.parquet')
    hashes = {p.name:sha256_file(p) for p in folder.iterdir()}
    evidence = {'output_sha256':hashes, 'candidates':[{'input_sdf_index':1,
        'compound_id':'control_afatinib','engine_status':'SUCCESS',
        'gate':{'physical_validity':'PASS','physical_evidence_complete':True}}]}
    spec = SimpleNamespace(run_id='artificial-control', parameters={
        'denovo_control_readback':review.freeze(), 'covdock_readback':{'fixture':True}})
    manager = SimpleNamespace(registry=SimpleNamespace(job_dir=lambda _:folder.parent))
    def registered(root, folder, spec, job, name, digest):
        review.Files(folder).read(name, digest)
        return folder/name
    monkeypatch.setattr(md_preparation_evidence, 'registered_output', registered)
    monkeypatch.setattr(review.covdock_readback, 'validate', lambda *args:{'runtime':{}})
    def sdk(request, runtime, script):
        assert request['pose_index'] == 0 and request['score'] == -10
        assert request['files'] == [str(folder/'container.mae')]
        return {'version':request['version'], 'input':request['files'][0],
            'scientific_quality_verified':False,'pose_count':1,'pose':pose}
    monkeypatch.setattr(review, '_covdock_sdk', sdk)
    return tmp_path, manager, spec, evidence, pose, folder, sdk


@pytest.mark.parametrize('physical,complete,expected', [
    ('PASS',True,'PASS'), ('FAIL',True,'FAIL'), ('UNKNOWN',False,'EVIDENCE_INSUFFICIENT'),
    ('PASS',False,'EVIDENCE_INSUFFICIENT')])
def test_registered_readback_requires_control_physical_gate_too(recorded_control, physical, complete, expected):
    root, manager, spec, evidence, _, _, _ = recorded_control
    evidence['candidates'][0]['gate'] = {'physical_validity':physical,'physical_evidence_complete':complete}
    measured = review.evaluate(root, root, manager, spec, None, evidence)
    assert measured['state'] == expected
    assert measured['rmsd_validation'] == 'PASS' and measured['heavy_atom_rmsd_A'] < 1e-10
    assert not measured['md_ready'] and not measured['scientific_result_verified']


def test_registered_control_catches_container_changed_during_readback(recorded_control, monkeypatch):
    root, manager, spec, evidence, _, folder, sdk = recorded_control
    def changed(*args):
        result = sdk(*args)
        (folder/'container.mae').write_text('changed during readback')
        return result
    monkeypatch.setattr(review, '_covdock_sdk', changed)
    with pytest.raises(ValueError, match='checksum differs'):
        review.evaluate(root, root, manager, spec, None, evidence)


def test_registered_control_wrong_vendor_response_is_not_measured(recorded_control, monkeypatch):
    root, manager, spec, evidence, _, _, sdk = recorded_control
    def wrong(*args):
        return {**sdk(*args), 'pose_count':2}
    monkeypatch.setattr(review, '_covdock_sdk', wrong)
    with pytest.raises(ValueError, match='response differs'):
        review.evaluate(root, root, manager, spec, None, evidence)
