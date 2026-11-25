"""Versioned EGFR fixed-acrylamide structural acceptance, never an activity predictor."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import yaml
from rdkit import Chem
from rdkit.Chem import AllChem, Descriptors
from rdkit.Chem.Scaffolds import MurckoScaffold
from posebusters import PoseBusters
from scipy.optimize import minimize

ROOT = Path(__file__).resolve().parent
SMOKE = ROOT / 'assets'
RULES = json.loads((ROOT / 'control/rules.json').read_text())
PB_CONFIG = yaml.safe_load((ROOT / 'control/posebusters.yml').read_text())
PB_KEYS = ['mol_pred_loaded', 'sanitization', 'inchi_convertible', 'all_atoms_connected',
           'no_radicals', 'bond_lengths', 'bond_angles', 'internal_steric_clash',
           'aromatic_ring_flatness', 'non-aromatic_ring_non-flatness',
           'double_bond_flatness', 'internal_energy']
PRE = Chem.MolFromMolFile(str(SMOKE / 'acrylamide_prereaction.sdf'))
POST = Chem.MolFromMolFile(str(SMOKE / 'acrylamide_adduct_fragment_named.sdf'))

def clean(value):
    if isinstance(value, dict): return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [clean(v) for v in value]
    if isinstance(value, np.ndarray): return clean(value.tolist())
    if isinstance(value, np.generic): return clean(value.item())
    if isinstance(value, float) and not math.isfinite(value): return None
    if isinstance(value, Path): return str(value)
    return value

def save_json(path, data):
    Path(path).write_text(json.dumps(clean(data), ensure_ascii=False, indent=2, allow_nan=False)+'\n')

def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def protein(path):
    lines = [l for l in Path(path).read_text().splitlines()
             if l.startswith('ATOM  ') and l[21] == 'A' and l[16] in ' A'
             and l[76:78].strip().upper() not in ['H', 'D']]
    keys = [(l[21], int(l[22:26]), l[26].strip(), l[12:16].strip()) for l in lines]
    if len(set(keys)) != len(keys): raise ValueError('Ambiguous protein atom identity')
    pos = np.array([[float(l[30:38]), float(l[38:46]), float(l[46:54])] for l in lines])
    elements = [l[76:78].strip().title() for l in lines]
    return {'keys': keys, 'pos': pos, 'elements': elements,
            'sg': keys.index(('A',797,'','SG')), 'cb': keys.index(('A',797,'','CB'))}

def angle(a,b,c):
    u,v=np.asarray(a)-b,np.asarray(c)-b
    den=np.linalg.norm(u)*np.linalg.norm(v)
    return float(np.degrees(np.arccos(np.clip(np.dot(u,v)/den,-1,1)))) if den>1e-8 else float('nan')

def within(value, bounds): return bool(np.isfinite(value) and bounds[0] <= value <= bounds[1])

def core_check(mol, state, mapped=None, enforce_growth=True, enforce_fixed=True):
    """Core order is always N, C=O, O, alpha-C, beta-C; no coordinate realignment."""
    ref = PRE if state == 'pre' else POST
    pos=mol.GetConformer().GetPositions()
    if mapped is None and state=='adduct': mapped=[4,3,1,2,0]  # checked exporter mapping
    if mapped is None:
        matches=mol.GetSubstructMatches(Chem.MolFromSmarts('[NX3][CX3](=[OX1])[CX3]=[CX3]'))
        if matches: mapped=min(matches,key=lambda m:np.mean((pos[list(m)]-ref.GetConformer().GetPositions())**2))
    if mapped is None or len(mapped)!=5 or max(mapped)>=mol.GetNumAtoms():
        return {'passed':False,'failures':['warhead_mapping_missing'],'mapping':None}
    mapped=list(mapped)
    actual={(i,j):mol.GetBondBetweenAtoms(mapped[i],mapped[j]).GetBondTypeAsDouble()
            for i in range(5) for j in range(i+1,5) if mol.GetBondBetweenAtoms(mapped[i],mapped[j])}
    expected={(0,1):1.,(1,2):2.,(1,3):1.,(3,4):2. if state=='pre' else 1.}
    graph=actual==expected and [mol.GetAtomWithIdx(i).GetSymbol() for i in mapped]==['N','C','O','C','C']
    external=[(i,n.GetIdx()) for i in mapped for n in mol.GetAtomWithIdx(i).GetNeighbors() if n.GetIdx() not in mapped]
    growth=len(external)==1 and external[0][0]==mapped[0]
    rmsd=float(np.sqrt(np.mean(np.sum((pos[mapped]-ref.GetConformer().GetPositions())**2,axis=1))))
    failures=[]
    if not graph: failures.append('warhead_graph_changed')
    if enforce_growth and not growth: failures.append('growth_not_N_only')
    if enforce_fixed and rmsd>RULES['fixed_core_rmsd_max_A']: failures.append('fixed_core_coordinates_changed')
    if enforce_growth and mol.GetNumHeavyAtoms()<=5: failures.append('no_new_heavy_atoms')
    return {'passed':not failures,'failures':failures,'mapping':mapped,'core_graph_preserved':graph,
            'only_N_growth':growth,'external_bonds':external,'rmsd_without_alignment_A':rmsd,
            'fixed_coordinates_required':enforce_fixed}

def covalent_geometry(mol, prot, core, state):
    mapping=core.get('mapping')
    if mapping is None: return {'passed':False,'failures':['covalent_mapping_missing']}
    p=mol.GetConformer().GetPositions();a,b=mapping[3:5];sg=prot['pos'][prot['sg']];cb=prot['pos'][prot['cb']]
    d=float(np.linalg.norm(p[b]-sg));ab=angle(sg,p[b],p[a]);sb=angle(cb,sg,p[b])
    checks={'SG_beta_distance':within(d,RULES[f'{state}_SG_beta_distance_A']),
            'SG_beta_alpha_angle':within(ab,RULES[f'{state}_SG_beta_alpha_angle_deg'])}
    if state=='adduct':
        checks['CB_SG_beta_angle']=within(sb,RULES['adduct_CB_SG_beta_angle_deg'])
        # Validate the external bond after adding methylthio surrogate at actual CB/SG.
        try:
            rw=Chem.RWMol(mol)
            atom=rw.GetAtomWithIdx(b)
            # RemoveHs may leave an explicit H count; replace the capping H by S.
            if atom.GetNumExplicitHs()>0: atom.SetNumExplicitHs(atom.GetNumExplicitHs()-1)
            iS=rw.AddAtom(Chem.Atom('S'));iC=rw.AddAtom(Chem.Atom('C'))
            rw.AddBond(b,iS,Chem.BondType.SINGLE);rw.AddBond(iS,iC,Chem.BondType.SINGLE)
            Chem.SanitizeMol(rw)
            checks['external_covalent_valence']=sum(x.GetNumRadicalElectrons() for x in rw.GetAtoms())==0
        except Exception:
            checks['external_covalent_valence']=False
    return {'passed':all(checks.values()),'failures':[f'covalent_{k}' for k,v in checks.items() if not v],
            'SG_beta_distance_A':d,'SG_beta_alpha_angle_deg':ab,'CB_SG_beta_angle_deg':sb,'checks':checks}

def clashes(mol, prot, core, state):
    p=mol.GetConformer().GetPositions();table=Chem.GetPeriodicTable()
    lr=np.array([table.GetRvdw(a.GetAtomicNum()) for a in mol.GetAtoms()])
    pr=np.array([table.GetRvdw(a) for a in prot['elements']])
    distances=np.linalg.norm(p[:,None,:]-prot['pos'][None,:,:],axis=-1)
    ratios=distances/(lr[:,None]+pr[None,:]);cut=np.full(ratios.shape,RULES['protein_clash_vdw_ratio_min'])
    exempt=[];scaled=[]
    if state=='adduct' and core.get('mapping') is not None:
        beta=core['mapping'][4];top=Chem.GetDistanceMatrix(mol)[beta]
        # Number of protein bonds from SG within the covalently connected Cys residue.
        pd={'SG':0,'CB':1,'CA':2,'N':3,'C':3,'O':4}
        for j,key in enumerate(prot['keys']):
            if key[:3]!=('A',797,'') or key[3] not in pd: continue
            lengths=top+1+pd[key[3]]
            for i in np.where(lengths<=2)[0]: cut[i,j]=0;exempt.append([int(i),int(j)])
            for i in np.where(lengths==3)[0]: cut[i,j]=RULES['adduct_14_vdw_ratio_min'];scaled.append([int(i),int(j)])
    bad=np.argwhere(ratios<cut)
    details=[{'ligand_atom':int(i),'protein_atom':list(prot['keys'][j]),'distance_A':float(distances[i,j]),
              'vdw_ratio':float(ratios[i,j]),'cutoff':float(cut[i,j])} for i,j in bad]
    details.sort(key=lambda r:r['vdw_ratio'])
    return {'passed':len(bad)==0,'failures':['protein_clashes'] if len(bad) else [],'n_clashes':len(bad),
            'min_distance_A':float(distances.min()),'min_unexcluded_vdw_ratio':float(ratios[cut>0].min()),
            'covalent_12_13_excluded_pairs':exempt,'covalent_14_scaled_pairs':scaled,'clashes':details,
            'contact_residues_within_4A':sorted(set(f'{prot["keys"][j][0]}:{prot["keys"][j][1]}' for j in np.where(distances.min(axis=0)<4)[0]))}

def evaluate(mol, prot, state, mapped=None, enforce_growth=True, enforce_fixed=None):
    if mol is None: return {'passed':False,'failures':['unparseable_molecule']}
    if enforce_fixed is None: enforce_fixed=state=='pre'
    try:
        Chem.SanitizeMol(mol)
        if mol.GetNumConformers()!=1 or not np.isfinite(mol.GetConformer().GetPositions()).all():
            raise ValueError('missing or nonfinite coordinates')
        core=core_check(mol,state,mapped,enforce_growth,enforce_fixed)
        geo=covalent_geometry(mol,prot,core,state);clash=clashes(mol,prot,core,state)
        pb=PoseBusters(copy.deepcopy(PB_CONFIG),max_workers=0).bust(Chem.Mol(mol),full_report=True)
        if len(pb)!=1: raise ValueError('PoseBusters did not return exactly one record')
        pb=clean(pb.iloc[0].to_dict())
        pb_fail=[f'posebusters_{k}' for k in PB_KEYS if pb.get(k) is not True]
        failures=core['failures']+geo['failures']+clash['failures']+pb_fail
        smiles=Chem.MolToSmiles(mol,isomericSmiles=True)
        return clean({'passed':not failures,'failures':failures,'smiles':smiles,
                     'scaffold':MurckoScaffold.MurckoScaffoldSmiles(mol=mol),
                     'MW':Descriptors.MolWt(mol),'heavy_atoms':mol.GetNumHeavyAtoms(),
                     'formal_charge':Chem.GetFormalCharge(mol),'core':core,'covalent_geometry':geo,
                     'protein_clashes':clash,'posebusters':pb})
    except Exception as e:
        return {'passed':False,'failures':['evaluation_error'],'error':repr(e)}

def relax(mol, core, prot=None, state='pre'):
    if mol is None or core.get('mapping') is None:
        return None,{'status':'NOT_ATTEMPTED','reason':'unparseable_or_unmapped'}
    if len(Chem.GetMolFrags(mol))!=1:
        return None,{'status':'NOT_ATTEMPTED','reason':'disconnected_graph_requires_chemical_redesign'}
    try:
        m=Chem.AddHs(Chem.Mol(mol),addCoords=True)
        if not AllChem.UFFHasAllMoleculeParams(m): raise ValueError('UFF parameter missing')
        ff=AllChem.UFFGetMoleculeForceField(m)
        c=RULES['relaxation'];nheavy=mol.GetNumAtoms();initial=m.GetConformer().GetPositions().copy()
        movable=np.array([i for i in range(m.GetNumAtoms()) if i not in core['mapping']])
        target=None
        if prot is not None:
            table=Chem.GetPeriodicTable()
            lr=np.array([table.GetRvdw(a.GetAtomicNum()) for a in mol.GetAtoms()])
            pr=np.array([table.GetRvdw(a) for a in prot['elements']])
            target=c['repulsion_vdw_ratio']*(lr[:,None]+pr[None,:])
            if state=='adduct':
                measured=clashes(mol,prot,core,state)
                for i,j in measured['covalent_12_13_excluded_pairs']: target[i,j]=0
                for i,j in measured['covalent_14_scaled_pairs']:
                    target[i,j]=RULES['adduct_14_vdw_ratio_min']*(lr[i]+pr[j])
        def objective(x):
            pos=initial.copy();pos[movable]=x.reshape(-1,3);flat=pos.reshape(-1).tolist()
            energy=ff.CalcEnergy(flat);grad=np.asarray(ff.CalcGrad(flat)).reshape(-1,3)
            delta=pos[:nheavy]-initial[:nheavy];k=c['heavy_atom_restraint_k_kcal_mol_A2']
            energy+=0.5*k*np.sum(delta**2);grad[:nheavy]+=k*delta
            if target is not None:
                vec=pos[:nheavy,None,:]-prot['pos'][None,:,:]
                dist=np.maximum(np.linalg.norm(vec,axis=-1),1e-8);gap=np.minimum(dist-target,0)
                kr=c['protein_repulsion_k_kcal_mol_A2'];energy+=0.5*kr*np.sum(gap**2)
                grad[:nheavy]+=np.sum((kr*gap/dist)[:,:,None]*vec,axis=1)
            return float(energy),grad[movable].reshape(-1)
        before=ff.CalcEnergy();initial_objective=objective(initial[movable].reshape(-1))[0]
        result=minimize(objective,initial[movable].reshape(-1),jac=True,method='L-BFGS-B',
                        options={'maxiter':c['max_iterations'],'gtol':c['force_tolerance'],'ftol':c['energy_tolerance'],'maxls':40})
        final=initial.copy();final[movable]=result.x.reshape(-1,3)
        for i,pos in enumerate(final): m.GetConformer().SetAtomPosition(i,pos)
        after=ff.CalcEnergy(final.reshape(-1).tolist());m=Chem.RemoveHs(m)
        displacement=np.linalg.norm(m.GetConformer().GetPositions()-mol.GetConformer().GetPositions(),axis=1)
        return m,{'status':'CONVERGED' if result.success else 'NOT_CONVERGED','returncode':int(result.status),
                  'optimizer_message':str(result.message),'iterations':int(result.nit),
                  'initial_objective_kcal_mol':initial_objective,'final_objective_kcal_mol':float(result.fun),
                  'UFF_before_kcal_mol':before,'UFF_after_kcal_mol':after,
                  'heavy_atom_rms_displacement_A':float(np.sqrt(np.mean(displacement**2))),
                  'max_heavy_atom_displacement_A':float(displacement.max()),
                  'method':c['method']}
    except Exception as e: return None,{'status':'ERROR','error':repr(e)}
