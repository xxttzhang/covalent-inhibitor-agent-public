import json
from pathlib import Path

import pytest

from covscreen.peptide_templates import standard_orientation, chirality_terms, peptide_reference


@pytest.mark.parametrize('code,center,expected', [('C', 'CA', 'R'), ('I', 'CA', 'S'),
    ('I', 'CB', 'S'), ('T', 'CA', 'S'), ('T', 'CB', 'R'), ('A', 'CA', 'S')])
def test_standard_stereochemical_exceptions(code, center, expected):
    label, neighbors, sign = standard_orientation(code, center)
    assert label == expected
    assert len(set(neighbors)) == 3
    assert sign in (-1, 1)


def test_ile_names_match_standard_sidechain_connectivity():
    mol, _ = peptide_reference('AIA')
    atom = next(a for a in mol.GetAtoms() if a.GetPDBResidueInfo().GetName().strip() == 'CD1')
    assert [a.GetPDBResidueInfo().GetName().strip() for a in atom.GetNeighbors()] == ['CG1']


def test_terms_bind_real_inventory_without_using_proposed_coordinates():
    path = Path(__file__).resolve().parents[2] / 'reports/library-first-site-acquisition-20261014/heavy-repair/atom-mapping.json'
    atoms = json.loads(path.read_text())['atoms']
    terms = chirality_terms(atoms)
    assert len(terms) == 350
    assert all(len(set(t['particles'])) == 4 for t in terms)
    for a in atoms:
        a['xyz_angstrom'] = [999, 999, 999]
    assert chirality_terms(atoms) == terms
    for term in terms:
        assert term['movable'] == any(atoms[i]['original_serial'] is None for i in term['particles'])


@pytest.mark.parametrize('code,center', [('G', 'CA'), ('A', 'CB'), ('X', 'CA')])
def test_unsupported_centers_rejected(code, center):
    with pytest.raises(ValueError):
        standard_orientation(code, center)
