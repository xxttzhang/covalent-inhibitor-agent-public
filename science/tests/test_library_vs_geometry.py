"""Library gates retain missing evidence and use actual product topology."""
from copy import deepcopy

import pytest
from rdkit import Chem

from covscreen.library_vs_geometry import Policy, assess, clashes, contact_sites, contacts
from covscreen.covdock_vs_output import inspect_complex
from test_covdock_vs_output import complex_fixture


def test_physical_pass_cannot_replace_strain_or_reference_contact_evidence():
    metrics = {'covalent_cs_distance_angstrom': 1.8, 'product_s_cbeta_calpha_angle_degrees': 110.,
        'clash_fraction': 0., 'severe_clash_pair_count': 0,
        'ligand_local_relaxation_energy_kcal_mol': None, 'reference_contact_site_retention': None}
    value = assess(metrics, Policy())
    assert value['physical_validity'] == 'PASS'
    assert value['status'] == 'UNKNOWN' and not value['review_eligible']
    assert set(value['missing_reasons']) == {'ligand_local_strain'}
    metrics.update(ligand_local_relaxation_energy_kcal_mol=30., reference_contact_site_retention=1.)
    assert assess(metrics, Policy())['failure_reasons'] == ['ligand_local_strain']
    metrics.update(ligand_local_relaxation_energy_kcal_mol=2., reference_contact_site_retention=0.)
    assert assess(metrics, Policy())['review_eligible']
    assert assess(metrics, Policy(version='library-vs-geometry-v1-provisional'))['failure_reasons'] == ['reference_contact_site_retention']
    metrics['reference_contact_site_retention'] = None
    assert assess(metrics, Policy())['review_eligible']
    metrics['reference_contact_site_retention'] = 1.
    assert assess(metrics, Policy())['review_eligible']
    metrics['severe_clash_pair_count'] = 1
    assert not assess(metrics, Policy())['review_eligible']


def test_clash_exclusions_follow_bonds_not_whole_cysteine_residue():
    original, prepared, receptor, output = complex_fixture()
    row = inspect_complex(output, original, prepared, receptor, 'A:797', 0)
    ligand, protein = row['ligand'], row['receptor']
    # Put the bound beta on SG: the 1-2 pair itself is not nonbonded.
    beta = next(a.GetIdx() for a in ligand.GetAtoms() if a.GetAtomMapNum() == 906)
    sg = next(a.GetIdx() for a in protein.GetAtoms() if a.GetAtomicNum() == 16)
    ligand.GetConformer().SetAtomPosition(beta, protein.GetConformer().GetAtomPosition(sg))
    before = clashes(ligand, protein, 'A:797', Policy())[0]
    editable = Chem.RWMol(protein)
    extra = Chem.Atom(7)
    extra.SetMonomerInfo(Chem.AtomPDBResidueInfo(atomName=' N  ', residueName='CYS', residueNumber=797, chainId='A'))
    added = editable.AddAtom(extra)
    modified = editable.GetMol()
    modified.GetConformer().SetAtomPosition(added, ligand.GetConformer().GetAtomPosition(beta))
    assert clashes(ligand, modified, 'A:797', Policy())[0] > before


def test_contact_proxy_uses_frozen_protein_site_not_a_shared_ligand_scaffold(tmp_path):
    reference = Chem.MolFromSmiles('N')
    conf = Chem.Conformer(1)
    conf.SetAtomPosition(0, (0., 0., 0.))
    reference.AddConformer(conf)
    sdf = tmp_path / 'reference.sdf'
    with Chem.SDWriter(str(sdf)) as writer:
        writer.write(reference)
    protein = Chem.RWMol()
    for number, xyz in [(10, (3., 0., 0.)), (11, (10., 0., 0.))]:
        atom = Chem.Atom(8)
        atom.SetMonomerInfo(Chem.AtomPDBResidueInfo(atomName=' O  ', residueName='ALA', residueNumber=number, chainId='A'))
        protein.AddAtom(atom)
    conf = Chem.Conformer(2)
    conf.SetAtomPosition(0, (3., 0., 0.)); conf.SetAtomPosition(1, (10., 0., 0.))
    protein.AddConformer(conf)
    protein = protein.GetMol()
    pdb = tmp_path / 'receptor.pdb'
    pdb.write_text(Chem.MolToPDBBlock(protein))
    record = contact_sites(sdf, pdb, Policy())
    assert len(record['sites']) == 1 and record['sites'][0]['protein_atom'] == ['A', 10, 'ALA', 'O']
    assert not record['hydrogen_bonds_assigned']
    observations, fraction = contacts(reference, protein, record, Policy())
    assert fraction == 1. and observations[0]['distance_angstrom'] == pytest.approx(3.)
    reference.GetConformer().SetAtomPosition(0, (-5., 0., 0.))
    assert contacts(reference, protein, record, Policy())[1] == 0.
    missing = deepcopy(record); missing['sites'] = []
    assert contacts(reference, protein, missing, Policy())[1] is None


@pytest.mark.parametrize('reference', [None, {'status': 'UNAVAILABLE'}, {'sites': [{}]}])
def test_optional_contacts_fail_independently(reference):
    from covscreen.library_vs_geometry import contact_observation
    molecule = Chem.MolFromSmiles('C')
    observations, retention, reason = contact_observation(molecule, molecule, reference, Policy())
    assert observations == [] and retention is None and reason
    with pytest.raises((KeyError, TypeError, ValueError)):
        contact_observation(molecule, molecule, reference, Policy(version='library-vs-geometry-v1-provisional'))
