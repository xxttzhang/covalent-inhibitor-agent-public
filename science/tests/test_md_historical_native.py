"""Synthetic atom graphs exercise native audit invariants, without vendor calls."""
from copy import deepcopy
import json
from types import SimpleNamespace as Item

import pytest

from covscreen.md_historical_native import inspect, original_atoms
from covscreen.md_historical_pose import GRAPH_POLICY


class Structure:
    def __init__(self, atoms, pairs, title='synthetic'):
        self.atom = atoms
        self.title = title
        self.property = {}
        indexed = {a.index: a for a in atoms}
        self.bond = [Item(atom1=indexed[i], atom2=indexed[j], order=order) for i, j, order in pairs]

    def extract(self, keep):
        atoms = [deepcopy(a) for a in self.atom if a.index in keep]
        mapping = {a.index: i + 1 for i, a in enumerate(atoms)}
        for atom in atoms:
            atom.index = mapping[atom.index]
        pairs = [(mapping[b.atom1.index], mapping[b.atom2.index], b.order) for b in self.bond
                 if b.atom1.index in mapping and b.atom2.index in mapping]
        return Structure(atoms, pairs, self.title)


@pytest.fixture
def native_case(tmp_path):
    atoms = []
    for residue, name in [(1, 'ALA'), (2, 'CYS')]:
        for offset, atom_name in enumerate(('N', 'CA', 'C', 'O')):
            atoms.append(Item(index=len(atoms) + 1, atomic_number=7 if atom_name == 'N' else 8 if atom_name == 'O' else 6,
                formal_charge=0, chain='A', resnum=residue, inscode='', pdbres=name, pdbname=atom_name,
                xyz=((residue - 1) * 3.9 + offset * 1.3, 0., 0.)))
    atoms.append(Item(index=9, atomic_number=6, formal_charge=0, chain='Z', resnum=10,
        inscode='', pdbres='OLD', pdbname='C1', xyz=(50., 0., 0.)))
    original = Structure(atoms, [(3, 5, 1)])
    structures = {'original-pose.maegz': [original], 'extracted-pose.mae': [deepcopy(original)],
        'original-loop-input.mae': [deepcopy(original)], 'adduct-input.mae': [original.extract(range(1, 9))]}
    folder = tmp_path / 'history'; folder.mkdir()
    (folder / 'source-manifest.json').write_text(json.dumps({'records': [{'system_id': 'fixture', 'title': 'synthetic'}]}))
    (folder / 'construct.fasta').write_text('>synthetic\nAC\n')
    (folder / 'cleanup.json').write_text(json.dumps({'removed_atoms': [9], 'removed_residues': ['Z:10:OLD']}))
    loaded = {'directory': tmp_path, 'manifest': {'pose_index': 0, 'compound_id': 'fixture', 'input_sdf_index': 0,
        'candidate_id': 'synthetic-fixture', 'parents': {'historical_source_sha256': 'a' * 64, 'target_identity_sha256': 'b' * 64},
        'target': {'author_chain': 'A', 'author_residue': 2, 'canonical_cys': 2}},
        'history': {'manifest': {'identity': {'system_id': 'fixture', 'recorded_construct': {
            'construct_start': 1, 'construct_end': 2, 'missing_internal_residues': []}}}}}
    return loaded, structures, lambda path: structures[path.rsplit('/', 1)[-1]]


def test_native_audit_replays_original_cleanup_and_actual_residues(native_case):
    loaded, _, reader = native_case
    _, count, audit = inspect(loaded, reader)
    assert count == 1 and audit['protein_residue_count'] == 2
    assert audit['native_coordinate_mapping_verified'] is True
    assert audit['parameterization_completed'] is False


@pytest.mark.parametrize('damage', ['changed_original', 'changed_loop', 'changed_cleanup', 'wrong_sequence', 'broken_peptide'])
def test_native_audit_rejects_real_graph_inconsistencies(native_case, damage):
    loaded, structures, reader = native_case
    if damage == 'changed_original':
        structures['extracted-pose.mae'][0].atom[0].formal_charge = 1
    elif damage == 'changed_loop':
        structures['original-loop-input.mae'][0].atom[0].xyz = (99., 0., 0.)
    elif damage == 'changed_cleanup':
        structures['adduct-input.mae'][0].atom[0].xyz = (99., 0., 0.)
    elif damage == 'wrong_sequence':
        for items in structures.values():
            for atom in items[0].atom:
                if atom.resnum == 2:
                    atom.pdbres = 'ALA'
    elif damage == 'broken_peptide':
        for items in structures.values():
            items[0].bond = []
    with pytest.raises(ValueError, match='Historical native graph'):
        inspect(loaded, reader)


@pytest.mark.parametrize('residue,name', [(720, 'C'), (720, 'O'), (725, 'N'),
                                         (746, 'C'), (746, 'O'), (757, 'N')])
def test_gap_junction_movement_is_explicit_and_legacy_remains_strict(residue, name):
    key = ('A', residue, '', 'SER', name)
    atom = Item(atomic_number=6, formal_charge=0, chain='A', resnum=residue,
                inscode='', pdbres='SER', pdbname=name, xyz=(1., 2., 3.))
    shifted = deepcopy(atom); shifted.xyz = (1., 2., 3.2)
    missing = set(range(721, 725)) | set(range(747, 757))
    moved = original_atoms({key: atom}, {key: shifted}, 'A', missing, GRAPH_POLICY)
    assert moved[0]['identity'] == list(key)
    assert moved[0]['displacement_A'] == pytest.approx(.2)
    with pytest.raises(ValueError, match='outside a permitted peptide junction'):
        original_atoms({key: atom}, {key: shifted}, 'A', missing, None)


@pytest.mark.parametrize('damage', ['ligand', 'sidechain', 'core', 'charge', 'nan'])
def test_junction_policy_does_not_exempt_other_changes(damage):
    atom = Item(atomic_number=6, formal_charge=0, chain='A', resnum=720,
                inscode='', pdbres='SER', pdbname='C', xyz=(1., 2., 3.))
    if damage == 'ligand': atom.chain = 'L'
    if damage == 'sidechain': atom.pdbname = 'CB'
    if damage == 'core': atom.resnum = 719
    key = (atom.chain, atom.resnum, '', atom.pdbres, atom.pdbname)
    shifted = deepcopy(atom); shifted.xyz = (1., 2., 3.2)
    if damage == 'charge': shifted.formal_charge = 1
    if damage == 'nan': shifted.xyz = (float('nan'), 2., 3.)
    with pytest.raises(ValueError, match='Historical native graph'):
        original_atoms({key: atom}, {key: shifted}, 'A', {721, 722, 723, 724}, GRAPH_POLICY)
