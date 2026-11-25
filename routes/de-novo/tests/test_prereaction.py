"""Real RDKit/Meeko/PoseBusters checks; synthetic poses are never science results."""
import json
from pathlib import Path

import numpy as np
import pytest
from rdkit import Chem
from rdkit.Chem import AllChem, rdDepictor

from covscreen_de_novo import prereaction as science
from covscreen_de_novo import prereaction_runner as runner


def test_runtime_detects_transitive_data_and_versioned_shared_library_changes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    library = tmp_path/'libfixture.so.1'
    data = tmp_path/'template.dat'
    library.write_bytes(b'artificial library version one')
    data.write_bytes(b'artificial scientific data')
    fake = SimpleNamespace(metadata={'Name': 'fixture-transitive-dependency'},
        files=[library.name, data.name], locate_file=lambda entry: tmp_path/entry)
    monkeypatch.setattr(runner.importlib.metadata, 'distributions', lambda: [fake])
    monkeypatch.setattr(runner.importlib.metadata, 'version', lambda name: 'fixture-version')
    first = runner.runtime()
    assert str(library) in first['files'] and str(data) in first['files']
    library.write_bytes(b'artificial library version two')
    assert runner.runtime() != first


def ligand(smiles='C=CC(=O)NC'):
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    return mol


def pose(smiles='C=CC(=O)NC'):
    return Chem.RemoveHs(science.independent_conformer(ligand(smiles)))


def test_frozen_policy_preserves_registered_thresholds_and_limits():
    value = science.freeze()
    assert value['policy']['sg_beta_distance_A'] == [2.8, 4.0]
    assert value['policy']['minimum_nonbonded_vdw_ratio'] == 0.75
    assert value['docking']['n_poses'] == 20
    assert value['docking']['cpu'] == 1
    assert not value['policy']['reactivity_calibrated']
    value['policy']['sg_beta_distance_A'][0] = 0
    assert science.freeze()['policy']['sg_beta_distance_A'][0] == 2.8


def test_embedding_removes_any_previous_coordinates():
    original = ligand()
    changed = Chem.Mol(original)
    for index in range(changed.GetNumAtoms()):
        changed.GetConformer().SetAtomPosition(index, [100+index, -100, 250])
    first = science.independent_conformer(original)
    second = science.independent_conformer(changed)
    assert np.allclose(first.GetConformer().GetPositions(), second.GetConformer().GetPositions())
    assert science.identity(first) == science.identity(original)


def test_reaction_mapping_uses_graph_not_nearest_atom():
    source = ligand()
    science.reaction_atoms(source, 1, 0)
    with pytest.raises(ValueError, match='unreacted acrylamide'):
        science.reaction_atoms(source, 0, 1)
    with pytest.raises(ValueError, match='unreacted acrylamide'):
        science.reaction_atoms(ligand('CCC(=O)NC'), 1, 0)


def test_substituted_control_does_not_expand_candidate_scope():
    source = ligand('C/C=C/C(=O)NC')
    science.reaction_atoms(source, 2, 1, control=True)
    with pytest.raises(ValueError, match='terminal acrylamide'):
        science.reaction_atoms(source, 2, 1)


def test_full_graph_mapping_tolerates_atom_reordering():
    source, moving = ligand(), pose()
    reordered = Chem.RenumberAtoms(moving, list(reversed(range(moving.GetNumAtoms()))))
    actual, mapping, atoms = science.map_pose(source, reordered, 1, 0)
    assert atoms == (mapping[1], mapping[0])
    assert science.identity(actual) == science.identity(source)


def test_full_graph_rejects_remote_change():
    with pytest.raises(ValueError, match='graph, charge'):
        science.map_pose(ligand(), pose('C=CC(=O)NCC'), 1, 0)


def test_stereo_requires_explicit_source_and_matching_3d_coordinates():
    with pytest.raises(ValueError, match='stereochemistry must be specified'):
        science.identity(ligand('C=CC(=O)NC(C)F'))
    source = ligand('C=CC(=O)N[C@H](C)F')
    moving = pose('C=CC(=O)N[C@H](C)F')
    science.map_pose(source, moving, 1, 0)
    xyz = moving.GetConformer().GetPositions()
    for index, point in enumerate(xyz):
        moving.GetConformer().SetAtomPosition(index, [-point[0], point[1], point[2]])
    with pytest.raises(ValueError, match='coordinates disagree'):
        science.map_pose(source, moving, 1, 0)


def test_nonfinite_coordinates_cannot_pass():
    moving = pose()
    moving.GetConformer().SetAtomPosition(0, [float('nan'), 0, 0])
    with pytest.raises(ValueError, match='nonfinite'):
        science.map_pose(ligand(), moving, 1, 0)


def test_nonbonded_target_residue_is_not_exempt():
    moving = pose()
    xyz = moving.GetConformer().GetPositions()
    # Artificial protein with one atom exactly overlapping a distal ligand atom.
    protein = {'keys': [('A',797,'','SG'), ('A',797,'','CB')],
        'xyz': np.asarray([xyz[0]+[0,0,3.2],xyz[-1]]), 'numbers':[16,6], 'sg':0,'cb':1}
    result = science.geometry(moving, protein, 1, 0)
    assert result['excluded_pairs'] == 0
    assert result['clash_count'] > 0
    assert not result['checks']['no_nonbonded_clashes']


def test_score_selection_precedes_geometry_and_preserves_census():
    source, first, second = ligand(), pose(), pose()
    for mol, offset in ((first, 500), (second, 800)):
        xyz = mol.GetConformer().GetPositions()
        for index, point in enumerate(xyz):
            mol.GetConformer().SetAtomPosition(index, point + offset)
    result = science.assess(source, [first, second], [-1.0,-2.0],
        science.receptor(science.REGISTRY/'assets/protein_A.pdb'), 1, 0)
    assert result['selected_pose_index'] == 1
    assert result['pose_count'] == 2
    assert result['state'] == 'FAIL'
    assert not result['md_admission_granted']
    assert all(not row['geometry']['checks']['distance'] for row in result['poses'])


@pytest.mark.parametrize('scores', [[], [float('nan')], [True]])
def test_incomplete_or_invalid_scores_reject(scores):
    with pytest.raises(ValueError):
        science.assess(ligand(), [pose()], scores,
            science.receptor(science.REGISTRY/'assets/protein_A.pdb'), 1, 0)


def test_registered_receptor_preparation_preserves_every_heavy_atom():
    pdbqt, result = runner.prepare_receptor(science.REGISTRY/'assets/protein_A.pdb')
    assert result['heavy_atoms'] == 2471
    assert result['residues'] == 307
    assert result['wanted_altloc'] == {'A:970':'A'}
    assert result['removed_residues'] == []
    assert result['target_template'] == 'CYS'
    assert pdbqt.startswith('ATOM')


def test_receptor_mutation_rejects_even_if_input_remains_valid_pdb(tmp_path):
    original = science.REGISTRY/'assets/protein_A.pdb'
    path = tmp_path/'receptor.pdb'
    path.write_text(original.read_text().replace('CYS A 797','SER A 797'))
    with pytest.raises(ValueError, match='registered'):
        science.receptor(path)


def test_meeko_ligand_roundtrip_preserves_identity():
    source = ligand('C=CC(=O)N[C@H](C)F')
    prepared = runner.prepare_ligand(source)
    # Format a synthetic one-pose Vina output; no docking result is claimed.
    poses, scores = runner.unpack('MODEL 1\nREMARK VINA RESULT: -1.0 0.0 0.0\n'+prepared+'ENDMDL\n')
    assert scores == [-1.0]
    assert len(poses) == 1
    science.map_pose(source, poses[0], 1, 0)
