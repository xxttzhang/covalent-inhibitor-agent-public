"""Freeze a declared small subset without reusing any downstream engine scores."""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
from rdkit import Chem, rdBase


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(mol):
    mol = Chem.Mol(mol)
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    return Chem.MolToSmiles(Chem.RemoveHs(mol), isomericSmiles=True)


def prepare(plan_path, source, output):
    plan = json.loads(plan_path.read_text())
    if plan['status'] != 'BUDGET_APPROVED_PREFLIGHT_IN_PROGRESS':
        raise ValueError('Recorded budget consent required; native execution remains separate')
    for kind in ('table', 'sdf'):
        path = source / Path(plan['source'][kind]).name
        if digest(path) != plan['source'][kind + '_sha256']:
            raise ValueError('Source digest mismatch: ' + kind)
    table = pd.read_parquet(source / Path(plan['source']['table']).name)
    supplier = Chem.SDMolSupplier(str(source / Path(plan['source']['sdf']).name), removeHs=False)
    if table.compound_id.duplicated().any() or len(plan['candidates']) != 3:
        raise ValueError('Expected three unique declared molecules')
    # Explicit input-only columns: never copy AF3, docking or ranking evidence.
    columns = ['compound_id', 'protomer_id', 'parent_id', 'target_id', 'site_id', 'label',
        'input_sdf_index', 'standardized_smiles', 'electrophilic_atom_map', 'warhead_class',
        'reaction_atom_index', 'carsidock_reaction_type', 'covdock_reaction_type',
        'chemistry_gate', 'parent_retained_fraction', 'property_fit_score',
        'molecular_weight', 'clogp', 'upstream_input_id', 'enumeration_manifest_sha256']
    rows, molecules, checks = [], [], []
    warhead = Chem.MolFromSmarts('[C]#[C]-[C](=[O])-[N]')
    for index, declared in enumerate(plan['candidates']):
        selected = table.loc[table.compound_id == declared['compound_id'], columns]
        if len(selected) != 1:
            raise ValueError('Missing/ambiguous compound identity')
        row = selected.iloc[0].to_dict()
        old_index = declared['source_input_sdf_index']
        if row['input_sdf_index'] != old_index:
            raise ValueError('Source record index mismatch')
        mol = supplier[old_index]
        if mol is None or mol.GetProp('_Name') != row['compound_id']:
            raise ValueError('SDF identity mismatch')
        smiles_mol = Chem.MolFromSmiles(row['standardized_smiles'])
        if smiles_mol is None or canonical(mol) != canonical(smiles_mol):
            raise ValueError('SDF and table chemistry disagree')
        mapped = [a.GetIdx() for a in mol.GetAtoms() if a.GetAtomMapNum() == 906]
        if mapped != [int(row['reaction_atom_index'])]:
            raise ValueError('Electrophile mapping/index mismatch')
        if mapped[0] not in [m[0] for m in mol.GetSubstructMatches(warhead)]:
            raise ValueError('Electrophile is not the carbonyl-activated alkyne beta carbon')
        row['source_input_sdf_index'] = old_index
        row['input_sdf_index'] = index
        mol.SetProp('SOURCE_INPUT_SDF_INDEX', str(old_index))
        mol.SetProp('INPUT_SDF_INDEX', str(index))
        rows.append(row)
        molecules.append(mol)
        checks.append({'compound_id': row['compound_id'], 'input_sdf_index': index,
            'source_input_sdf_index': old_index, 'canonical_smiles': canonical(mol),
            'reaction_atom_index': mapped[0], 'identity_and_electrophile_valid': True})
    output.mkdir(parents=True, exist_ok=False)
    sdf = output / 'candidates.sdf'
    with Chem.SDWriter(str(sdf)) as writer:
        for mol in molecules:
            writer.write(mol)
    frame = pd.DataFrame(rows)
    frame.to_parquet(output / 'candidates.parquet', index=False)
    frame.to_csv(output / 'candidates.csv', index=False)
    manifest = {'schema': 'ligand-smoke-inputs-v1', 'test_id': plan['test_id'],
        'plan_sha256': digest(plan_path), 'source': plan['source'], 'rdkit_version': rdBase.rdkitVersion,
        'checks': checks, 'reused_generation': True, 'reused_engine_scores': False,
        'compute_submitted': False,
        'files': {p.name: digest(p) for p in sorted(output.iterdir())}}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.plan, args.source, args.output), indent=2))
