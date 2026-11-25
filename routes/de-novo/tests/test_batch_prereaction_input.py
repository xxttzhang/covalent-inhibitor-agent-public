import pytest
from rdkit import Chem
from covscreen_de_novo.batch_prereaction_input import fresh, export, identity


def test_fresh_free_ligand_preserves_graph_stereo_and_reaction_atoms(tmp_path):
    original = {'candidate_id': 'PXM_fixture', 'parent_id': 'generation:0:raw',
        'mapped_smiles': 'C[C@H](N)CNC(=O)[CH:905]=[CH2:906]'}
    molecule, row = fresh(original)
    assert molecule.GetConformer().Is3D()
    assert identity(molecule) == identity(Chem.MolFromSmiles(original['mapped_smiles']))
    bond = molecule.GetBondBetweenAtoms(row['beta_index_0based'], row['alpha_index_0based'])
    assert bond.GetBondType() == Chem.BondType.DOUBLE
    assert not row['lo_coordinates_used'] and not row['generated_coordinates_used']
    result = export([original], tmp_path / 'fresh')
    assert result['states'][0]['state'] == 'PREPARED_NOT_DOCKED'
    assert not result['docking_performed']
    with pytest.raises(FileExistsError):
        export([original], tmp_path / 'fresh')


def test_missing_warhead_is_deferred_without_silent_replacement(tmp_path):
    result = export([{'candidate_id': 'bad', 'parent_id': 'bad', 'mapped_smiles': 'CC'}], tmp_path / 'bad')
    assert result['states'][0]['state'] == 'DEFERRED'
    assert 'acrylamide' in result['states'][0]['reason']
    assert not list((tmp_path / 'bad').glob('*.sdf'))


def test_prereaction_freeze_requires_a_real_registered_lo_source(tmp_path):
    from covscreen_de_novo.batch_prereaction_input import freeze
    with pytest.raises(ValueError, match='Exact LO input digest'):
        freeze(tmp_path.resolve(), '../foreign')
    with pytest.raises(ValueError, match='regular immutable data'):
        freeze(tmp_path.resolve(), 'a' * 64)
    assert not (tmp_path / 'state/de-novo-prereaction-inputs').exists()


def test_prereaction_load_rejects_missing_or_tampered_manifest_before_work(tmp_path):
    from covscreen_de_novo.batch_prereaction_input import load
    root = tmp_path.resolve()
    with pytest.raises(ValueError, match='already exist'):
        load(root, 'a' * 64, 'b' * 64)
    folder = root / 'state/de-novo-prereaction-inputs' / ('a' * 64)
    folder.mkdir(parents=True)
    (folder / 'input.json').write_text('{}')
    with pytest.raises(ValueError, match='frozen approval input'):
        load(root, 'a' * 64, 'b' * 64)
    assert list(folder.iterdir()) == [folder / 'input.json']


def test_reference_is_fresh_official_precursor_with_reproducible_grid_center(tmp_path):
    from covscreen_de_novo.batch_prereaction_input import prepare_reference
    value = prepare_reference(tmp_path / 'reference')
    assert value['candidate_id'] == 'CONTROL_AFATINIB'
    assert value['prereaction_pose_reference_available'] is False
    assert value['grid_center_from_crystal_adduct'] == pytest.approx(
        [50.60173529411765, 1.5936176470588235, -20.669911764705883])
    molecule = next(Chem.SDMolSupplier(str(tmp_path / 'reference/fresh_afatinib.sdf'), removeHs=False))
    assert identity(molecule) == value['canonical_smiles']
    assert molecule.GetBondBetweenAtoms(value['beta_index_0based'], value['alpha_index_0based']).GetBondType() == Chem.BondType.DOUBLE


def test_priority_requires_physical_evidence_but_not_af3():
    from covscreen_de_novo.batch_priority import select
    originals = [{'candidate_id': str(i), 'parent_id': str(i), 'input_index': i} for i in range(7)]
    candidates = [{'compound_id': str(i), 'input_sdf_index': i, 'engine_status': 'SUCCESS',
        'covdock_score': -10 + i, 'gate': {'physical_validity': 'PASS', 'physical_evidence_complete': True,
            'cross_method_consensus': 'NA', 'review_eligible': False}} for i in range(7)]
    candidates[0]['gate']['physical_validity'] = 'FAIL'
    result = select({'candidates': candidates}, originals)
    assert [row['candidate_id'] for row in result['selected']] == ['1', '2', '3', '4', '5']
    assert result['deferred'][0]['candidate_id'] == '0'
    assert result['deferred'][1]['candidate_id'] == '6'
    candidates[1]['covdock_score'] = None
    assert select({'candidates': candidates}, originals)['selected'][0]['candidate_id'] == '2'
