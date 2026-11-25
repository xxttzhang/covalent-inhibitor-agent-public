"""Conservative identity QC for JSONL attempts; never a pose/activity gate."""
import argparse
import json
from pathlib import Path
from rdkit import Chem, rdBase
from rdkit.Chem import Descriptors
from rdkit.Chem.Scaffolds import MurckoScaffold

WARHEAD = '[CH2:1]=[CH:2]-[C:3](=[O:4])-[N:5]'

def assess(rows: list[dict]) -> dict:
    query = Chem.MolFromSmarts(WARHEAD)
    seen: dict[str, str] = {}
    results = []
    ids = [r['attempt_id'] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate attempt IDs')
    for row in rows:
        result = dict(row, reasons=[], duplicate_of=None)
        mol = Chem.MolFromSmiles(row.get('smiles') or '')
        if mol is None or mol.GetNumAtoms() == 0:
            result['reasons'].append('INVALID_OR_EMPTY_SMILES')
        else:
            invalid_aromatic = [bond.GetIdx() for bond in mol.GetBonds()
                if bond.GetBondType() == Chem.BondType.AROMATIC
                and (not bond.IsInRing() or not bond.GetIsAromatic()
                     or not bond.GetBeginAtom().GetIsAromatic() or not bond.GetEndAtom().GetIsAromatic())]
            result['inconsistent_aromatic_bonds'] = invalid_aromatic
            if invalid_aromatic:
                result['reasons'].append('INCONSISTENT_AROMATIC_BONDS')
            key = Chem.MolToSmiles(mol, isomericSmiles=True)
            result['canonical_smiles'] = key
            result['duplicate_of'] = seen.get(key)
            seen.setdefault(key, row['attempt_id'])
            if result['duplicate_of']:
                result['reasons'].append('DUPLICATE')
            if len(Chem.GetMolFrags(mol)) != 1:
                result['reasons'].append('DISCONNECTED_COMPONENTS')
            matches = mol.GetSubstructMatches(query)
            result['warhead_matches'] = [dict(beta=m[0], alpha=m[1], carbonyl=m[2], oxygen=m[3], nitrogen=m[4]) for m in matches]
            if len(matches) != 1:
                result['reasons'].append('WARHEAD_MISSING_OR_AMBIGUOUS')
            unresolved = [str(x.centeredOn) for x in Chem.FindPotentialStereo(mol) if str(x.specified) == 'Unspecified']
            result['unresolved_stereochemistry'] = unresolved
            if unresolved:
                result['reasons'].append('UNRESOLVED_STEREOCHEMISTRY')
            result['molecular_weight'] = Descriptors.MolWt(mol)
            result['scaffold'] = MurckoScaffold.MurckoScaffoldSmiles(mol=mol)
        result['identity_eligible'] = not result['reasons']
        results.append(result)
    return {'rdkit': rdBase.rdkitVersion, 'policy': 'identity-aromatic-consistency-v2', 'warhead_smarts': WARHEAD,
            'scope': 'identity only; atom indices refer to parsed input SMILES; no activity or pose claims',
            'attempted': len(rows), 'identity_eligible': sum(x['identity_eligible'] for x in results), 'rows': results}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    with args.output.open('x') as stream:
        json.dump(assess(rows), stream, indent=2)
