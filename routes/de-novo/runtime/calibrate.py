"""Reference and injected-fault calibration before generating repeat batches."""
import shlex
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "registry/egfr_c797_v1"))
from pathlib import Path
import numpy as np
from rdkit import Chem
from evaluator import ROOT, SMOKE, PRE, POST, protein, evaluate, relax, save_json, digest

ROOT = Path(sys.argv[1]).resolve()
ROOT.mkdir(parents=True, exist_ok=True)

def ccd_loop(path, prefix):
    lines=path.read_text().splitlines();keys=[];rows=[];active=False
    for line in lines:
        if line.startswith(prefix): keys.append(line.strip());active=True
        elif active:
            if line.startswith('#'): break
            if line.strip(): rows.append(shlex.split(line))
    return [dict(zip(keys,row)) for row in rows]

def reference():
    path=SMOKE/'0WN.cif'
    atoms=ccd_loop(path,'_chem_comp_atom.');bonds=ccd_loop(path,'_chem_comp_bond.')
    lines=(SMOKE/'4G5J.pdb').read_text().splitlines()
    coords={l[12:16].strip():[float(l[30:38]),float(l[38:46]),float(l[46:54])]
            for l in lines if l.startswith('HETATM') and l[17:20]=='0WN' and l[21:26]=='A1101'}
    rw=Chem.RWMol();lookup={}
    for a in atoms:
        if a['_chem_comp_atom.type_symbol']=='H': continue
        name=a['_chem_comp_atom.atom_id'];atom=Chem.Atom(a['_chem_comp_atom.type_symbol'].title())
        atom.SetFormalCharge(int(a['_chem_comp_atom.charge']));atom.SetProp('reference_atom_name',name)
        lookup[name]=rw.AddAtom(atom)
    for b in bonds:
        x,y=b['_chem_comp_bond.atom_id_1'],b['_chem_comp_bond.atom_id_2']
        if x in lookup and y in lookup:
            rw.AddBond(lookup[x],lookup[y],{'SING':Chem.BondType.SINGLE,'DOUB':Chem.BondType.DOUBLE,'TRIP':Chem.BondType.TRIPLE}[b['_chem_comp_bond.value_order']])
    mol=rw.GetMol();Chem.SanitizeMol(mol);conf=Chem.Conformer(mol.GetNumAtoms())
    for k,i in lookup.items(): conf.SetAtomPosition(i,coords[k])
    mol.AddConformer(conf);Chem.AssignStereochemistryFrom3D(mol)
    mol.SetProp('_Name','4G5J_0WN_A1101_isolated_hydrogen_capped_adduct')
    Chem.MolToMolFile(mol,str(ROOT/'4G5J_0WN_adduct.sdf'))
    return mol,[lookup[x] for x in ['N26','C27','O29','C28','C30']]

def main():
    out=ROOT/'calibration';out.mkdir(exist_ok=True)
    prot=protein(SMOKE/'protein_A.pdb')
    # Minimal N-methyl growth control. Constructed before looking at repeat outputs.
    rw=Chem.RWMol(PRE);i=rw.AddAtom(Chem.Atom('C'));rw.AddBond(0,i,Chem.BondType.SINGLE)
    mol=rw.GetMol();Chem.SanitizeMol(mol);p=PRE.GetConformer().GetPositions()
    mol.GetConformer().SetAtomPosition(i,p[0]+(p[0]-p[1])/np.linalg.norm(p[0]-p[1])*1.46)
    mol,optimization=relax(mol,{'mapping':list(range(5))},prot);assert mol is not None
    mol.SetProp('_Name','constructed_N_methyl_acrylamide_positive')
    Chem.MolToMolFile(mol,str(out/'positive_pre.sdf'))
    cases=[]
    def test(name,m,state='pre',expected_failure=None,prot_arg=None,mapping=None,reference_mode=False):
        Chem.MolToMolFile(m,str(out/(name+'.sdf')))
        result=evaluate(m,prot_arg or prot,state,mapping,
                        enforce_growth=not reference_mode,enforce_fixed=False if reference_mode else None)
        if expected_failure is None: ok=result['passed']
        else: ok=any(f==expected_failure for f in result['failures'])
        row={'name':name,'expectation':'all_pass' if expected_failure is None else expected_failure,
             'calibration_pass':ok,'result':result};cases.append(row)
        print(name,ok,result['failures'],flush=True)
    test('positive_pre',mol)
    m=Chem.Mol(mol);m.GetConformer().SetAtomPosition(5,np.array(m.GetConformer().GetAtomPosition(5))+[8,0,0])
    test('broken_bond_length',m,expected_failure='posebusters_bond_lengths')
    m=Chem.Mol(mol);m.GetConformer().SetAtomPosition(5,m.GetConformer().GetAtomPosition(2))
    test('internal_overlap',m,expected_failure='posebusters_internal_steric_clash')
    m=Chem.RWMol(mol);m.RemoveBond(0,5);m.AddBond(4,5,Chem.BondType.SINGLE);Chem.SanitizeMol(m)
    test('wrong_growth_anchor',m.GetMol(),expected_failure='growth_not_N_only')
    m=Chem.RWMol(mol);m.GetBondBetweenAtoms(3,4).SetBondType(Chem.BondType.SINGLE);Chem.SanitizeMol(m)
    test('lost_acrylamide_double_bond',m.GetMol(),expected_failure='warhead_mapping_missing')
    m=Chem.RWMol(mol);m.RemoveBond(0,5);Chem.SanitizeMol(m)
    test('disconnected',m.GetMol(),expected_failure='posebusters_all_atoms_connected')
    m=Chem.Mol(mol)
    for i in range(m.GetNumAtoms()): m.GetConformer().SetAtomPosition(i,np.array(m.GetConformer().GetAtomPosition(i))+[20,0,0])
    test('translated_core',m,expected_failure='fixed_core_coordinates_changed')
    test('wrong_site_distance',m,expected_failure='covalent_SG_beta_distance')
    m=Chem.Mol(mol);m.GetConformer().SetAtomPosition(5,prot['pos'][0])
    test('protein_overlap',m,expected_failure='protein_clashes')
    badprot={**prot,'pos':prot['pos'].copy()};p=mol.GetConformer().GetPositions()
    vector=p[3]-p[4];badprot['pos'][prot['sg']]=p[4]+vector/np.linalg.norm(vector)*3.3
    test('wrong_approach_angle',mol,expected_failure='covalent_SG_beta_alpha_angle',prot_arg=badprot)
    m=Chem.Mol(mol);m.RemoveAllConformers()
    missing=evaluate(m,prot,'pre')
    cases.append({'name':'missing_coordinates','expectation':'fail_closed','calibration_pass':not missing['passed'],'result':missing})
    none=evaluate(None,prot,'pre')
    cases.append({'name':'unparseable_input','expectation':'fail_closed','calibration_pass':not none['passed'],'result':none})
    ref,mapping=reference()
    # Experimental ligand is beta-substituted; it is a geometry control, not an N-only-growth control.
    rr=evaluate(ref,prot,'adduct',mapping,enforce_growth=False,enforce_fixed=False)
    cases.append({'name':'experimental_adduct_reference','expectation':'external_covalent_geometry_pass',
                  'calibration_pass':rr.get('covalent_geometry',{}).get('passed',False),'result':rr})
    print('experimental_adduct_reference',cases[-1]['calibration_pass'],rr['failures'],flush=True)
    badprot={**prot,'pos':prot['pos'].copy()};beta=ref.GetConformer().GetPositions()[mapping[4]]
    direction=prot['pos'][prot['sg']]-beta;badprot['pos'][prot['sg']]=beta+direction/np.linalg.norm(direction)*1.2
    test('short_covalent_bond',ref,'adduct','covalent_SG_beta_distance',badprot,mapping,True)
    # A collision elsewhere on Cys must not disappear with covalent exclusions.
    badprot={**prot,'pos':prot['pos'].copy()};badprot['pos'][prot['cb']]=ref.GetConformer().GetPositions()[0]
    test('nonbonded_overlap_on_target_residue',ref,'adduct','protein_clashes',badprot,mapping,True)
    save_json(out/'results.json',{'passed':all(c['calibration_pass'] for c in cases),'n_cases':len(cases),
                                 'positive_control_optimization':optimization,'cases':cases,
                                 'reference_source':'https://files.rcsb.org/ligands/download/0WN.cif',
                                 'reference_ccd_sha256':digest(SMOKE/'0WN.cif'),
                                 'rules_sha256':digest(SMOKE.parent/'control/rules.json')})
    if not all(c['calibration_pass'] for c in cases): raise SystemExit('Calibration failed; do not freeze or launch repeats')

if __name__=='__main__': main()
