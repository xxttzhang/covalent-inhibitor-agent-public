"""Reconstruct a fresh afatinib redocking control from primary CCD/PDB files.
No docking is run; the crystal adduct is not a prereaction reference.
"""
import argparse
import hashlib
import json
import shlex
from pathlib import Path
from rdkit import Chem, rdBase
from rdkit.Chem import AllChem

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def loop(path, prefix):
    keys, rows = [], []
    for line in path.read_text().splitlines():
        if line.startswith(prefix):
            keys.append(line.strip())
        elif keys:
            if line.startswith('#'):
                break
            if line.strip():
                rows.append(dict(zip(keys, shlex.split(line))))
    return rows


def write_mol(mol, path):
    with Chem.SDWriter(str(path)) as writer:
        writer.write(mol)
    check = Chem.SDMolSupplier(str(path), removeHs=True)[0]
    assert check is not None
    assert Chem.MolToSmiles(check) == Chem.MolToSmiles(mol)


def prepare_control(assets, precursor_ccd, expected_precursor_sha256, inputs):
    if sha(precursor_ccd) != expected_precursor_sha256:
        raise ValueError('Precursor CCD checksum differs')
    inputs.mkdir(parents=True, exist_ok=False)
    # CCD defines the complete ligand graph; crystal ATOM coordinates define the
    # reference pose. Keep author atom names in a separate lossless map.
    ccd = assets / '0WN.cif'
    coord = {line[12:16].strip(): [float(line[30:38]), float(line[38:46]), float(line[46:54])]
             for line in (assets / '4G5J.pdb').read_text().splitlines()
             if line.startswith('HETATM') and line[17:20] == '0WN' and line[21:26] == 'A1101'}
    rw, lookup = Chem.RWMol(), {}
    for row in loop(ccd, '_chem_comp_atom.'):
        if row['_chem_comp_atom.type_symbol'] == 'H':
            continue
        atom = Chem.Atom(row['_chem_comp_atom.type_symbol'].title())
        atom.SetFormalCharge(int(row['_chem_comp_atom.charge']))
        lookup[row['_chem_comp_atom.atom_id']] = rw.AddAtom(atom)
    for row in loop(ccd, '_chem_comp_bond.'):
        a, b = row['_chem_comp_bond.atom_id_1'], row['_chem_comp_bond.atom_id_2']
        if a in lookup and b in lookup:
            rw.AddBond(lookup[a], lookup[b], {'SING': Chem.BondType.SINGLE, 'DOUB': Chem.BondType.DOUBLE,
                                            'TRIP': Chem.BondType.TRIPLE}[row['_chem_comp_bond.value_order']])
    ref = rw.GetMol()
    Chem.SanitizeMol(ref)
    conf = Chem.Conformer(ref.GetNumAtoms())
    for name, idx in lookup.items():
        conf.SetAtomPosition(idx, coord[name])
    ref.AddConformer(conf)
    Chem.AssignStereochemistryFrom3D(ref)
    ref.SetProp('_Name', 'control_4G5J_0WN_crystal_hydrogen_capped')
    write_mol(ref, inputs / 'control_crystal_adduct.sdf')
    (inputs / 'control_atom_map.json').write_text(json.dumps(lookup, indent=2)+'\n')
    pre = Chem.RWMol(ref)
    bond = pre.GetBondBetweenAtoms(lookup['C28'], lookup['C30'])
    assert bond.GetBondType() == Chem.BondType.SINGLE
    bond.SetBondType(Chem.BondType.DOUBLE)
    # Afatinib precursor is the E alkene; the sole oxolane stereocenter is
    # retained from the crystal/CCD and checked against the official 0WM graph.
    ends = {lookup['C28']: lookup['C27'], lookup['C30']: lookup['C31']}
    bond.SetStereoAtoms(ends[bond.GetBeginAtomIdx()], ends[bond.GetEndAtomIdx()])
    bond.SetStereo(Chem.BondStereo.STEREOE)
    pre = pre.GetMol()
    pre.RemoveAllConformers()
    Chem.SanitizeMol(pre)
    Chem.AssignStereochemistry(pre, cleanIt=False, force=True)
    official = [row for row in loop(precursor_ccd, '_pdbx_chem_comp_descriptor.')
                if row['_pdbx_chem_comp_descriptor.type'] == 'SMILES_CANONICAL']
    official_smiles = {Chem.MolToSmiles(Chem.MolFromSmiles(row['_pdbx_chem_comp_descriptor.descriptor']))
                       for row in official}
    assert Chem.MolToSmiles(pre) in official_smiles, (Chem.MolToSmiles(pre), official_smiles)
    AllChem.Compute2DCoords(pre)
    pre.SetProp('_Name', 'control_afatinib')
    write_mol(pre, inputs / 'control_afatinib.sdf')
    record = dict(id='control_afatinib', role='redocking_control', input_file='control_afatinib.sdf',
                  smiles=Chem.MolToSmiles(pre), heavy_atoms=pre.GetNumHeavyAtoms(),
                  beta_index_1based=lookup['C30']+1, alpha_index_1based=lookup['C28']+1,
                  reference_file='control_crystal_adduct.sdf')
    manifest = dict(state='CONTROL_INPUTS_PREPARED_NOT_REDOCKED', target='EGFR WT 4G5J A:C797',
                    record=record, rdkit_version=rdBase.rdkitVersion,
                    sources={str(p):sha(p) for p in [ccd, precursor_ccd, assets/'4G5J.pdb']},
                    files={p.name:sha(p) for p in inputs.iterdir() if p.is_file()},
                    prereaction_pose_reference_available=False,
                    covalent_control_policy=dict(pose_selection='best vendor score before RMSD calculation',
                        max_heavy_atom_rmsd_A=2.0, alignment='receptor backbone, symmetry-aware ligand mapping; no ligand fitting'),
                    note='Crystal adduct is a covalent redocking reference, not an experimental prereaction pose.')
    (inputs/'control_manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return manifest

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--precursor-ccd', type=Path, required=True)
    parser.add_argument('--precursor-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = prepare_control(args.assets, args.precursor_ccd, args.precursor_sha256, args.output)
    print(json.dumps({'state': result['state'], 'smiles': result['record']['smiles']}))
