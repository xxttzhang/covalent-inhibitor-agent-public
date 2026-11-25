"""Post-LO computational priority, without substituting AF3 for physical evidence."""
import math


def select(review, originals):
    indexed = {row['candidate_id']: row for row in originals}
    rows, deferred, seen = [], [], set()
    for candidate in review['candidates']:
        name = candidate['compound_id']
        if name in seen or name not in indexed:
            raise ValueError('Post-LO review has duplicate or foreign candidates')
        seen.add(name)
        original = indexed[name]
        if candidate['input_sdf_index'] != original['input_index']:
            raise ValueError('Post-LO candidate index changed')
        gate = candidate['gate']
        score = candidate.get('covdock_score')
        reasons = []
        if candidate['engine_status'] != 'SUCCESS':
            reasons.append('ENGINE_NOT_SUCCESSFUL')
        if gate.get('physical_validity') != 'PASS' or gate.get('physical_evidence_complete') is not True:
            reasons.append('PHYSICAL_CHECKS_NOT_PASSED')
        if type(score) not in (float, int) or not math.isfinite(score):
            reasons.append('FINITE_LO_SCORE_MISSING')
        if reasons:
            deferred.append({'candidate_id': name, 'reasons': reasons})
        else:
            rows.append({'candidate_id': name, 'parent_id': original['parent_id'],
                'input_index': original['input_index'], 'lo_score': score})
    rows.sort(key=lambda row: (row['lo_score'], row['candidate_id']))
    selected = []
    parents = set()
    for row in rows:
        if len(selected) >= 5 or row['parent_id'] in parents:
            deferred.append({'candidate_id': row['candidate_id'], 'reasons': ['PRIORITY_BUDGET_OR_DUPLICATE_PARENT']})
            continue
        parents.add(row['parent_id'])
        selected.append(row)
    return {'schema': 'denovo-post-lo-priority-v1', 'selected': selected, 'deferred': deferred,
        'limit': 5, 'activity_validated': False, 'prereaction_evaluated': False,
        'rule': 'LO physical PASS and complete evidence; finite LO score ascending; five distinct parents; AF3 optional'}
