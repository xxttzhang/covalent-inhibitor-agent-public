"""Select at most twelve parents, at most two per scaffold, from verified VS."""
import math
from collections import defaultdict
from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold


def select(candidates, originals):
    indexed = {row['candidate_id']: row for row in originals}
    if len(indexed) != len(originals) or len({r['candidate_id'] for r in candidates}) != len(candidates):
        raise ValueError('Duplicate LO selection input identity')
    if set(indexed) != {row['candidate_id'] for row in candidates}:
        raise ValueError('LO selection requires the complete VS candidate denominator')
    buckets, deferred = defaultdict(list), []
    for candidate in candidates:
        name = candidate['candidate_id']
        original = indexed[name]
        if candidate['parent_id'] != original['parent_id']:
            raise ValueError('LO parent identity differs from generation')
        poses = [p for p in candidate['poses'] if p['geometry']['eligible_for_lo']
                 and p['product_identity_verified'] and p['prepared_receptor_identity_verified']]
        if not poses:
            deferred.append({'candidate_id': name, 'reason': 'NO_PASSING_VS_POSE'})
            continue
        if any(not isinstance(p['score'], (float, int)) or not math.isfinite(p['score']) for p in poses):
            raise ValueError('LO selection requires finite verified VS scores')
        pose = min(poses, key=lambda p: (p['score'], p['pose_index']))
        molecule = Chem.MolFromSmiles(original['mapped_smiles'])
        if molecule is None:
            raise ValueError('Invalid original LO molecular graph')
        for atom in molecule.GetAtoms():
            atom.SetAtomMapNum(0)
        smiles = Chem.MolToSmiles(molecule, isomericSmiles=True)
        scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=molecule) or 'acyclic:' + smiles
        buckets[scaffold].append({'candidate_id': name, 'parent_id': original['parent_id'],
            'vs_pose_index': pose['pose_index'], 'vs_score': pose['score'], 'scaffold': scaffold,
            'canonical_smiles': smiles, 'input_index': original['input_index']})
    key = lambda row: (row['vs_score'], row['canonical_smiles'], row['candidate_id'])
    for bucket in buckets.values():
        bucket.sort(key=key)
    selected = []
    for round_index in range(2):
        available = sorted((bucket[round_index] for bucket in buckets.values() if len(bucket) > round_index), key=key)
        selected.extend(available[:max(0, 12 - len(selected))])
    names = {row['candidate_id'] for row in selected}
    deferred.extend({'candidate_id': row['candidate_id'], 'reason': 'DIVERSITY_OR_LO_BUDGET'}
                    for bucket in buckets.values() for row in bucket if row['candidate_id'] not in names)
    return {'schema': 'denovo-batch-lo-selection-v1', 'limit': 12, 'max_per_scaffold': 2,
        'candidate_count': len(candidates), 'selected_count': len(selected), 'selected': selected,
        'deferred': sorted(deferred, key=lambda r: r['candidate_id']), 'compute_submitted': False,
        'selection_is_activity_ranking': False, 'lo_completed': False}
