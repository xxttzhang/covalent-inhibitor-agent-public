"""Artificial molecular graphs/coordinates; no generator, vendor docking, or MD."""
import copy
from pathlib import Path

import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from covscreen_de_novo import chemical_review as review


def example(smiles='C=CC(=O)N[C@H](F)C', *, return_product=False):
    precursor = Chem.MolFromSmiles(smiles)
    core = list(precursor.GetSubstructMatch(Chem.MolFromSmarts('[NX3][CX3](=[OX1])[CX3]=[CX3]')))
    capped, _ = review.reaction_expectation(precursor, core)
    rw = Chem.RWMol(capped)
    sulfur = rw.AddAtom(Chem.Atom('S')); cb = rw.AddAtom(Chem.Atom('C'))
    rw.AddBond(core[4],sulfur,Chem.BondType.SINGLE); rw.AddBond(sulfur,cb,Chem.BondType.SINGLE)
    product = rw.GetMol(); Chem.SanitizeMol(product)
    product = Chem.AddHs(product)
    assert AllChem.EmbedMolecule(product,randomSeed=42) == 0
    coordinates = product.GetConformer().GetPositions()
    atoms = [{'atom_name':f'A{i}', 'atom_index':i+1, 'atomic_number':a.GetAtomicNum(),
        'element':a.GetSymbol(), 'formal_charge':a.GetFormalCharge(), 'isotope':a.GetIsotope(),
        'xyz':coordinates[i].tolist(), 'hydrogen_count':sum(n.GetAtomicNum()==1 for n in product.GetAtomWithIdx(i).GetNeighbors()),
        'hydrogen_isotopes':[n.GetIsotope() for n in product.GetAtomWithIdx(i).GetNeighbors() if n.GetAtomicNum()==1]}
        for i,a in enumerate(capped.GetAtoms())]
    def record(index,name,chain='A',resnum=797,resname='CYS'):
        a=product.GetAtomWithIdx(index)
        return {'atom_index':index+1,'atom_name':name,'chain':chain,'resnum':resnum,'icode':'',
            'resname':resname,'atomic_number':a.GetAtomicNum(),'formal_charge':a.GetFormalCharge(),
            'isotope':a.GetIsotope(),'hydrogen_count':sum(n.GetAtomicNum()==1 for n in a.GetNeighbors())}
    sg=record(sulfur,'SG'); cb_atom=record(cb,'CB')
    beta=record(core[4],f'A{core[4]}',chain='L',resnum=1,resname='LIG')
    pose={'pose_index':0,'title':'candidate_000__p0__i0', 'ligand_heavy_atoms':atoms,
        'ligand_heavy_bonds':[{'atom_name_1':f'A{b.GetBeginAtomIdx()}',
            'atom_name_2':f'A{b.GetEndAtomIdx()}','order':b.GetBondTypeAsDouble()} for b in capped.GetBonds()],
        'covalent_partner_atom_name':f'A{core[4]}',
        'external_heavy_bonds':[{'ligand_atom_index':core[4]+1,'partner':sg,'order':1.}],
        'nucleophile':{'atom':copy.deepcopy(sg),'bonds':[{'atom':beta,'order':1.},{'atom':cb_atom,'order':1.}]}}
    return (precursor,core,pose,product) if return_product else (precursor,core,pose)


def test_full_reaction_identity_hydrogens_and_remote_stereo_preserved():
    precursor,core,pose=example()
    result=review.measure(precursor,core,pose)
    assert result['state']=='PASS'
    assert result['product_ligand_hydrogens']==result['precursor_ligand_hydrogens']+1
    assert result['beta_vendor_atom_index']==core[4]+1
    assert len(result['precursor_to_vendor_atom_indices'])==precursor.GetNumHeavyAtoms()
    assert result['source_stereo_coordinate_checks']
    assert not result['md_ready'] and not result['scientific_result_verified']


def test_atom_renaming_and_reordering_do_not_change_identity():
    precursor,core,pose=example('C=CC(=O)NCc1ccccc1')
    names={a['atom_name']:'renamed_'+a['atom_name'] for a in pose['ligand_heavy_atoms']}
    for a in pose['ligand_heavy_atoms']: a['atom_name']=names[a['atom_name']]
    for b in pose['ligand_heavy_bonds']:
        b['atom_name_1']=names[b['atom_name_1']]; b['atom_name_2']=names[b['atom_name_2']]
    pose['covalent_partner_atom_name']=names[pose['covalent_partner_atom_name']]
    pose['ligand_heavy_atoms'].reverse()
    assert review.measure(precursor,core,pose)['state']=='PASS'


def test_remote_element_change_is_not_hidden_by_correct_warhead():
    precursor,core,pose=example()
    fluorine=next(a for a in pose['ligand_heavy_atoms'] if a['element']=='F')
    fluorine.update(element='Cl',atomic_number=17)
    assert review.measure(precursor,core,pose)['state']=='FAIL'


def test_isotope_change_outside_warhead_fails():
    precursor,core,pose=example()
    pose['ligand_heavy_atoms'][-1]['isotope']=13
    assert review.measure(precursor,core,pose)['state']=='FAIL'


def test_mirrored_specified_stereochemistry_fails():
    precursor,core,pose=example()
    for a in pose['ligand_heavy_atoms']: a['xyz'][0]*=-1
    result=review.measure(precursor,core,pose)
    assert result['state']=='FAIL' and 'stereochemistry' in result['reason']


def test_unassigned_source_stereo_remains_unresolved():
    precursor,core,pose=example()
    Chem.RemoveStereochemistry(precursor)
    result=review.measure(precursor,core,pose)
    assert result['state']=='EVIDENCE_INSUFFICIENT'
    assert 'unspecified' in result['reason']


@pytest.mark.parametrize('change', ['second_link','wrong_residue','wrong_beta','double_link',
    'sulfur_hydrogen','sulfur_charge','extra_sulfur_neighbor','missing_ligand_hydrogen'])
def test_covalent_graph_errors_rejected(change):
    precursor,core,pose=example()
    if change=='second_link': pose['external_heavy_bonds'].append(copy.deepcopy(pose['external_heavy_bonds'][0]))
    if change=='wrong_residue': pose['external_heavy_bonds'][0]['partner']['resnum']=798
    if change=='wrong_beta': pose['external_heavy_bonds'][0]['ligand_atom_index']=core[3]+1
    if change=='double_link': pose['external_heavy_bonds'][0]['order']=2.
    if change=='sulfur_hydrogen': pose['external_heavy_bonds'][0]['partner']['hydrogen_count']=1
    if change=='sulfur_charge': pose['external_heavy_bonds'][0]['partner']['formal_charge']=-1
    if change=='extra_sulfur_neighbor': pose['nucleophile']['bonds'].append(copy.deepcopy(pose['nucleophile']['bonds'][0]))
    if change=='missing_ligand_hydrogen':
        pose['ligand_heavy_atoms'][core[3]]['hydrogen_count']-=1
        pose['ligand_heavy_atoms'][core[3]]['hydrogen_isotopes'].pop()
    assert review.measure(precursor,core,pose)['state']=='FAIL'


def test_wrong_reaction_atom_mapping_or_substituted_warhead_not_silently_accepted():
    precursor,core,pose=example()
    with pytest.raises(ValueError,match='recipe'):
        review.measure(precursor,[*core[:3],core[4],core[3]],pose)
    with pytest.raises(ValueError,match='recipe'):
        example('CC=CC(=O)N[C@H](F)C')


def test_missing_hydrogen_evidence_raises_instead_of_assigning_pass():
    precursor,core,pose=example()
    del pose['ligand_heavy_atoms'][0]['hydrogen_count']
    with pytest.raises(KeyError): review.measure(precursor,core,pose)


def test_plan_freezes_chemical_policy_and_coordinate_reader():
    binding=review.freeze(); review.validate(binding)
    binding['policy']['microstate']='silently neutralize'
    with pytest.raises(ValueError,match='changed after plan freeze'): review.validate(binding)


def test_unexpected_hydrogen_isotope_is_not_silently_dropped():
    precursor,core,pose=example()
    pose['ligand_heavy_atoms'][core[3]]['hydrogen_isotopes'][0]=2
    result=review.measure(precursor,core,pose)
    assert result['state']=='FAIL' and 'isotope' in result['reason']


def test_registered_evaluator_uses_exact_frozen_precursor(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    precursor,core,pose=example()
    folder=tmp_path/'independent';folder.mkdir()
    with Chem.SDWriter(str(folder/'candidate_000.sdf')) as writer: writer.write(precursor)
    # SDF writer adds 2D coordinates, as native source preparation does.
    (folder/'input_manifest.json').write_text(json.dumps({'records':[{'id':'candidate_000',
        'smiles':Chem.MolToSmiles(precursor,isomericSmiles=True),'core_indices_0based':core}]}))
    spec=SimpleNamespace(parameters={'denovo_chemical_readback':review.freeze()})
    evidence={'candidates':[{'input_sdf_index':0,'compound_id':'candidate_000','engine_status':'SUCCESS'}]}
    def read(*args,**kwargs):
        assert kwargs=={'input_index':0,'compound_id':'candidate_000','version':'covdock-candidate-chemistry-v1'}
        return pose,{'run_id':'artificial-run','pose_container_sha256':'a'*64}
    monkeypatch.setattr(review.control_review,'read_pose',read)
    monkeypatch.setattr(review.control_review,'revalidate_outputs',lambda *args:None)
    result=review.evaluate(tmp_path,tmp_path,None,spec,None,evidence)
    assert result['state']=='PASS' and result['precursor_sha256']==review.sha256_file(folder/'candidate_000.sdf')
    content=json.loads((folder/'input_manifest.json').read_text());content['records'][0]['smiles']='CC'
    (folder/'input_manifest.json').write_text(json.dumps(content))
    with pytest.raises(ValueError,match='identity differs'):
        review.evaluate(tmp_path,tmp_path,None,spec,None,evidence)


def test_remote_double_bond_stereochemistry_is_preserved():
    precursor,core,pose=example('C=CC(=O)N/C=C/C')
    assert review.measure(precursor,core,pose)['state']=='PASS'
    _,_,wrong=example('C=CC(=O)N/C=C\\C')
    assert review.measure(precursor,core,wrong)['state']=='FAIL'


def test_unreacted_double_bond_and_truncated_mapping_fail_closed(monkeypatch):
    precursor,core,pose=example()
    for bond in pose['ligand_heavy_bonds']:
        if {bond['atom_name_1'],bond['atom_name_2']}=={f'A{core[3]}',f'A{core[4]}'}: bond['order']=2.
    assert review.measure(precursor,core,pose)['state']=='FAIL'
    precursor,core,pose=example('C=CC(=O)NCc1ccccc1')
    monkeypatch.setattr(review,'MAX_MAPPINGS',1)
    assert review.measure(precursor,core,pose)['state']=='EVIDENCE_INSUFFICIENT'


@pytest.mark.parametrize('extra_link', [False, True])
def test_vendor_reader_extracts_complete_boundary_and_hydrogen_counts(monkeypatch,extra_link):
    """Exercise actual extractor against a scripted SDK surface, not a vendor run."""
    import importlib.util
    import sys
    from types import SimpleNamespace, ModuleType
    precursor,core,_,product=example(return_product=True)
    count=precursor.GetNumAtoms(); atoms=[]; bonds=[]
    for atom in product.GetAtoms():
        index=atom.GetIdx(); is_protein=index in (count,count+1)
        atoms.append(SimpleNamespace(index=index+1, atomic_number=atom.GetAtomicNum(),
            element=atom.GetSymbol(), formal_charge=atom.GetFormalCharge(), isotope=atom.GetIsotope(),
            xyz=tuple(product.GetConformer().GetAtomPosition(index)), bonded_atoms=[],
            chain='A' if is_protein else 'L',resnum=797 if is_protein else 1,
            pdbres='CYS' if is_protein else 'UNL',inscode='',
            pdbname=('SG' if index==count else 'CB') if is_protein else f'A{index}'))
    def add_bond(first,second,order):
        bonds.append(SimpleNamespace(atom1=first,atom2=second,order=order))
        first.bonded_atoms.append(second);second.bonded_atoms.append(first)
    for bond in product.GetBonds(): add_bond(atoms[bond.GetBeginAtomIdx()],atoms[bond.GetEndAtomIdx()],bond.GetBondTypeAsDouble())
    for i in range(3):
        atoms.append(SimpleNamespace(index=len(atoms)+1,atomic_number=6,element='C',formal_charge=0,isotope=0,
            xyz=(i,1,0),bonded_atoms=[],chain='A',resnum=790+i,pdbres='ALA',inscode='',pdbname='CA'))
    if extra_link:
        other=SimpleNamespace(index=len(atoms)+1,atomic_number=16,element='S',formal_charge=0,isotope=0,
            xyz=(1,2,3),bonded_atoms=[],chain='A',resnum=798,pdbres='CYS',inscode='',pdbname='SG')
        atoms.append(other);add_bond(atoms[core[0]],other,1.)
    structure=SimpleNamespace(atom=atoms,bond=bonds,atom_total=len(atoms),title='candidate_000__p0__i0',
        property={'r_cdock_cdock_affinity':-10})
    class Reader:
        def __init__(self,path): assert path=='/fixture.maegz'
        def __enter__(self): return iter([structure])
        def __exit__(self,*args): pass
    sdk=ModuleType('schrodinger.structure');sdk.StructureReader=Reader
    monkeypatch.setitem(sys.modules,'schrodinger',ModuleType('schrodinger'))
    monkeypatch.setitem(sys.modules,'schrodinger.structure',sdk)
    scripts=Path(__file__).resolve().parents[3]/'science/scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('chemical_vendor_fixture',scripts/'read_covdock_control.py')
    reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)
    request={'version':'covdock-candidate-chemistry-v1','files':['/fixture.maegz'],'attachment_residue':'A:797',
        'pose_index':0,'title':structure.title,'score_property':'r_cdock_cdock_affinity','score':-10}
    actual=reader.read_control(request)
    assert len(actual['pose']['external_heavy_bonds'])==(2 if extra_link else 1)
    result=review.measure(precursor,core,actual['pose'])
    assert result['state']==('FAIL' if extra_link else 'PASS')
    request['title']='control_afatinib__p0__i1'
    with pytest.raises(ValueError,match='fixed candidate/control role'): reader.read_control(request)
