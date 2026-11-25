"""Chemical selection of native, source-verified PocketXMol raw attempts."""
import argparse
import json
import sys
from pathlib import Path

from .batch_chemical_qc import assess
from .batch_diversity import select


def preparation_records(selection):
    """Fresh precursor graphs for LigPrep; preserve parent and reactive atom identity."""
    from rdkit import Chem
    from .batch_chemical_qc import WARHEAD
    query = Chem.MolFromSmarts(WARHEAD)
    records = []
    for row in selection['diversity']['selected']:
        mol = Chem.MolFromSmiles(row['canonical_smiles'])
        if mol is None:
            raise ValueError('Selected precursor is unreadable')
        matches = mol.GetSubstructMatches(query)
        if len(matches) != 1:
            raise ValueError('Selected precursor reaction site changed')
        # Recompute indices after canonicalization; QC indices use the original SMILES.
        beta, alpha, carbonyl, oxygen, nitrogen = matches[0]
        for atom in mol.GetAtoms():
            atom.SetAtomMapNum(atom.GetIdx() + 1)
        if mol.GetNumAtoms() >= 905:
            raise ValueError('Precursor exceeds reserved reaction mapping range')
        mol.GetAtomWithIdx(alpha).SetAtomMapNum(905)
        mol.GetAtomWithIdx(beta).SetAtomMapNum(906)
        records.append({
            'parent_id': row['attempt_id'],
            'source_sha256': row['source_sha256'],
            'mapped_smiles': Chem.MolToSmiles(mol, isomericSmiles=True),
            'reaction_atom_maps': dict(beta=906, alpha=905,
                carbonyl=carbonyl + 1, oxygen=oxygen + 1, nitrogen=nitrogen + 1),
            'generated_coordinates_reused': False,
        })
    return records


def prepare(results, limit=100):
    if results.get('state') != 'RESULTS_READ':
        raise ValueError('Read verified native generation results first')
    count = results.get('requested_slots')
    if type(count) is not int or count < 1:
        raise ValueError('Invalid requested slot count')
    raw = [row for row in results['candidates'] if row['representation'] == 'raw']
    if len(raw) != count or {row['slot'] for row in raw} != set(range(count)):
        raise ValueError('Raw candidates must cover every requested slot exactly once')
    rows = []
    for row in sorted(raw, key=lambda item: item['slot']):
        if row['candidate_id'] != f"{results['task_id']}:{row['slot']}:raw":
            raise ValueError('Candidate does not belong to this task and slot')
        if row.get('smiles') and not row.get('source_sha256'):
            raise ValueError('Parsed candidate lacks verified source identity')
        rows.append({
            'attempt_id': row['candidate_id'], 'smiles': row.get('smiles'),
            'source_sha256': row.get('source_sha256'), 'slot': row['slot'],
            'generation_geometry_passed': row['structural_qc_passed'],
            'generation_geometry_failures': row['failures'],
        })
    # Generation geometry is recorded; independent docking evaluates new coordinates.
    qc = assess(rows)
    return {
        'schema': 'pocketxmol-batch-selection-v1', 'task_id': results['task_id'],
        'provenance': results['provenance'], 'representation': 'raw',
        'chemical_qc': qc, 'diversity': select(qc, limit),
        'compute_submitted': False, 'independent_preparation': 'NOT_PERFORMED',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='JSON file, or - for verified host input on stdin')
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    content = sys.stdin.read() if args.input == '-' else Path(args.input).read_text()
    selection = prepare(json.loads(content), args.limit)
    selection['preparation_records'] = preparation_records(selection)
    print(json.dumps(selection))
