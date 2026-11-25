"""Actual Maestro round trips with synthetic products; no vendor runtime."""
import gzip

import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from covscreen.covdock_vs_output import read, inspect_complex, ordered_identity, expected_product
from covscreen.covdock_vs_geometry import Policy, assess, reference_frame
from covscreen.jobs import sha256_file
from receptor_fixture import receptor_molecule


def complex_fixture():
    original = Chem.MolFromSmiles('NC(=O)[CH:905]=[CH2:906]')
    original.SetProp('_Name','candidate-one')
    prepared = Chem.AddHs(original)
    assert AllChem.EmbedMolecule(prepared,randomSeed=11) == 0
    expected,beta,sg = expected_product(original)
    product = Chem.AddHs(expected)
    assert AllChem.EmbedMolecule(product,randomSeed=12) == 0
    # Explicit output atom lineage is indexed into the actual prepared input.
    for index,atom in enumerate(product.GetAtoms()):
        atom.SetMonomerInfo(Chem.AtomPDBResidueInfo(atomName=f'C{index+1:3}',
            residueName='UNK',residueNumber=1,chainId='L'))
        if index < sg:
            atom.SetIntProp('i_cdock_internal_original_atom_number',index+1)
    remove = [a.GetIdx() for a in product.GetAtomWithIdx(sg).GetNeighbors() if a.GetAtomicNum()==1]+[sg]
    fragment = Chem.RWMol(product)
    for index in sorted(remove,reverse=True):fragment.RemoveAtom(index)
    receptor = receptor_molecule()
    protein = Chem.RWMol(receptor)
    protein.RemoveAtom(2)  # HG is removed by the product reaction.
    offset = fragment.GetNumAtoms()
    combined = Chem.RWMol(Chem.CombineMols(fragment.GetMol(),protein.GetMol()))
    combined.AddBond(beta,offset+1,Chem.BondType.SINGLE)
    combined = combined.GetMol()
    combined.UpdatePropertyCache(strict=False)
    combined.SetProp('_Name','candidate-one')
    combined.SetIntProp('i_sd_INPUT_SDF_INDEX',0)
    combined.SetProp('s_sd_COMPOUND_ID','candidate-one')
    combined.SetDoubleProp('r_i_docking_score',-5.2)
    return original,prepared,receptor,combined


def test_read_actual_maestro_preserves_own_receptor_and_mapped_product(tmp_path):
    original,prepared,receptor,complex_mol = complex_fixture()
    path = tmp_path/'raw.mae'
    with Chem.MaeWriter(str(path)) as writer:writer.write(complex_mol)
    compressed = tmp_path/'raw.maegz'
    compressed.write_bytes(gzip.compress(path.read_bytes()))
    rows,counts = read(compressed,[original],[prepared],receptor,'A:797',expected_sha256=sha256_file(compressed))
    assert counts == {'candidate-one':1} and rows[0]['score'] == -5.2
    assert rows[0]['product_identity_verified'] and rows[0]['prepared_receptor_identity_verified']
    assert rows[0]['product'].GetNumAtoms() == original.GetNumHeavyAtoms()+1
    assert {a.GetAtomMapNum() for a in rows[0]['ligand'].GetAtoms()} >= {905,906}
    assert rows[0]['receptor'].GetNumAtoms() == 2


def test_explicit_reaction_rejection_preserves_raw_pose_ordinals_and_strict_default(tmp_path):
    original, prepared, receptor, good = complex_fixture()
    bad = Chem.Mol(good)
    bad.GetAtomWithIdx(3).SetIntProp('i_cdock_internal_original_atom_number', 5)
    bad.GetAtomWithIdx(4).SetIntProp('i_cdock_internal_original_atom_number', 4)
    path = tmp_path / 'mixed.mae'
    with Chem.MaeWriter(str(path)) as writer:
        for molecule in (bad, good):
            writer.write(molecule)
    with pytest.raises(ValueError, match='declared beta carbon'):
        read(path, [original], [prepared], receptor, 'A:797', expected_sha256=sha256_file(path))
    rejected = []
    rows, counts = read(path, [original], [prepared], receptor, 'A:797',
        expected_sha256=sha256_file(path), reaction_site_rejections=rejected)
    assert counts == {'candidate-one': 2}
    assert [row['pose_index'] for row in rows] == [1]
    assert rejected == [{'pose_index': 0, 'candidate_id': 'candidate-one', 'input_index': 0,
        'reason': 'VS target SG is not attached to the declared beta carbon',
        'product_identity_verified': False}]
    good.SetIntProp('i_sd_INPUT_SDF_INDEX', 9)
    with Chem.MaeWriter(str(path)) as writer:
        writer.write(good)
    with pytest.raises(ValueError, match='candidate index'):
        read(path, [original], [prepared], receptor, 'A:797',
            expected_sha256=sha256_file(path), reaction_site_rejections=[])


@pytest.mark.parametrize('mutation',['charge','lineage','candidate','index','beta','receptor','missing_lineage','coordinate','score','extra'])
def test_raw_output_rejects_changed_identity_or_coordinates(mutation):
    original,prepared,receptor,output = complex_fixture()
    if mutation=='charge':output.GetAtomWithIdx(0).SetFormalCharge(1)
    if mutation=='lineage':output.GetAtomWithIdx(0).SetIntProp('i_cdock_internal_original_atom_number',2)
    if mutation=='candidate':output.SetProp('s_sd_COMPOUND_ID','other')
    if mutation=='index':output.SetIntProp('i_sd_INPUT_SDF_INDEX',1)
    if mutation=='missing_lineage':output.GetAtomWithIdx(0).ClearProp('i_cdock_internal_original_atom_number')
    if mutation=='coordinate':output.GetConformer().SetAtomPosition(0,(float('nan'),0,0))
    if mutation=='score':output.SetDoubleProp('r_i_docking_score',float('inf'))
    if mutation=='receptor':receptor.GetAtomWithIdx(0).GetPDBResidueInfo().SetName(' XX ')
    if mutation=='beta':
        a,b = output.GetAtomWithIdx(3),output.GetAtomWithIdx(4)
        a.SetIntProp('i_cdock_internal_original_atom_number',5)
        b.SetIntProp('i_cdock_internal_original_atom_number',4)
    if mutation=='extra':
        rw = Chem.RWMol(output)
        atom = Chem.Atom(8)
        atom.SetMonomerInfo(Chem.AtomPDBResidueInfo(atomName=' O  ',residueName='HOH',residueNumber=2,chainId='L'))
        rw.AddAtom(atom);output=rw.GetMol()
    with pytest.raises((ValueError,RuntimeError)):
        inspect_complex(output,original,prepared,receptor,'A:797',0)


def test_preparation_cannot_reorder_atoms_even_with_same_canonical_identity():
    original,prepared,_,_ = complex_fixture()
    reordered = Chem.RenumberAtoms(prepared,list(reversed(range(prepared.GetNumAtoms()))))
    with pytest.raises(ValueError,match='order'):
        ordered_identity(original,reordered)


def test_missing_candidate_remains_in_denominator_and_changed_hash_is_rejected(tmp_path):
    original,prepared,receptor,output = complex_fixture()
    other = Chem.Mol(original);other.SetProp('_Name','candidate-two')
    other_prepared = Chem.Mol(prepared);other_prepared.SetProp('_Name','candidate-two')
    path=tmp_path/'raw.mae'
    with Chem.MaeWriter(str(path)) as writer:writer.write(output)
    _,counts=read(path,[original,other],[prepared,other_prepared],receptor,'A:797',expected_sha256=sha256_file(path))
    assert counts == {'candidate-one':1,'candidate-two':0}
    with pytest.raises(ValueError,match='checksum'):
        read(path,[original],[prepared],receptor,'A:797',expected_sha256='0'*64)


def test_zero_clash_does_not_bypass_missing_contacts_or_excess_strain():
    measured = {'scaffold_rmsd_angstrom':0.5,'interaction_retention_fraction':None,
        'strain_excess_over_parent_kcal_mol':20.,'covalent_cs_distance_angstrom':1.8,
        'product_s_cbeta_calpha_angle_degrees':110.,'clash_fraction':0.,'severe_clash_pair_count':0}
    value=assess(measured,Policy())
    assert value['physical_validity']=='PASS'
    assert value['design_constraints']=='FAIL'
    assert not value['review_eligible'] and not value['evidence_complete']
    assert value['missing_reasons']==['interaction_retention']
    assert value['failure_reasons']==['ligand_strain']
    measured.update(interaction_retention_fraction=0.8,strain_excess_over_parent_kcal_mol=2.)
    assert assess(measured,Policy())['review_eligible']
    measured['severe_clash_pair_count']=1
    assert not assess(measured,Policy())['review_eligible']


def test_reference_alignment_uses_receptor_not_ligand_and_preserves_handedness(tmp_path):
    import numpy as np
    mol=Chem.RWMol()
    xyz=[(0.,0.,0.),(1.,0.,0.),(0.,1.,0.),(0.,0.,1.)]
    conf=Chem.Conformer(4)
    for index,point in enumerate(xyz):
        atom=Chem.Atom(6)
        atom.SetMonomerInfo(Chem.AtomPDBResidueInfo(atomName=' CA ',residueName='ALA',residueNumber=index+1,chainId='A'))
        mol.AddAtom(atom);conf.SetAtomPosition(index,point)
    mol.AddConformer(conf)
    original=mol.GetMol();path=tmp_path/'projection.pdb'
    path.write_text(Chem.MolToPDBBlock(original))
    from covscreen.covdock_vs_geometry import transformed
    moved=transformed(original,np.array([[0.,-1.,0.],[1.,0.,0.],[0.,0.,1.]]),np.array([5.,6.,7.]))
    rotation,translation,report=reference_frame(moved,path)
    assert np.linalg.det(rotation)==pytest.approx(1.)
    assert report['rmsd_angstrom']<1e-8 and not report['ligand_fitted']
    assert np.allclose(moved.GetConformer().GetPositions() @ rotation+translation,xyz)


@pytest.mark.parametrize('invalid', ['coordinates','rotation','translation','overflow'])
def test_coordinate_transform_rejects_nonfinite_input_and_overflow(invalid):
    import numpy as np
    from covscreen.covdock_vs_geometry import transformed
    _,mol,_,_ = complex_fixture()
    rotation,translation = np.eye(3),np.zeros(3)
    if invalid == 'coordinates':
        mol.GetConformer().SetAtomPosition(0,(float('nan'),0.,0.))
    elif invalid == 'rotation':
        rotation[0,0] = float('inf')
    elif invalid == 'translation':
        translation[0] = float('nan')
    else:
        mol.GetConformer().SetAtomPosition(0,(1e308,0.,0.))
        rotation[0,0] = 1e308
    with pytest.raises(ValueError, match='coordinate transform'):
        transformed(mol,rotation,translation)
