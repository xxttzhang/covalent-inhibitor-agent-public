"""Deterministic evidence triage, not calibrated predictive uncertainty.

Scores are ranked separately with declared directions and average tied ranks.
No raw-score addition, imputation, stochastic uncertainty claim or GPU dispatch.
"""
import math

VERSION = 'rank-disagreement-triage-v1'
FIELDS = {'max_candidates', 'max_fraction', 'disagreement_threshold', 'top_fraction',
          'uncertainty_count', 'consensus_count', 'diversity_count'}
DIRECTIONS = {'higher_is_better', 'lower_is_better'}


def validate(policy):
    if not isinstance(policy, dict) or set(policy) != FIELDS:
        raise ValueError('All selection policy fields must be explicit; no extra options')
    for key in ('max_candidates', 'uncertainty_count', 'consensus_count', 'diversity_count'):
        if type(policy[key]) is not int or not 0 <= policy[key] <= 100:
            raise ValueError('Selection counts must be integers in [0,100]')
    if not 1 <= policy['max_candidates'] <= 100 or policy['uncertainty_count'] < 1:
        raise ValueError('A positive uncertainty budget and total cap are required')
    if sum(policy[key] for key in ('uncertainty_count', 'consensus_count', 'diversity_count')) > policy['max_candidates']:
        raise ValueError('Selection channel budgets exceed the total cap')
    for key in ('max_fraction', 'disagreement_threshold', 'top_fraction'):
        if type(policy[key]) not in (int, float) or not math.isfinite(policy[key]) or not 0 < policy[key] <= 1:
            raise ValueError('Selection thresholds must be finite fractions in (0,1]')
    if policy['max_fraction'] > 0.25:
        raise ValueError('Refinement must remain a small subset (at most 25% of eligible candidates)')
    return policy


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def ranks(rows, column, direction):
    if direction not in DIRECTIONS:
        raise ValueError('Score direction must be explicitly declared by each engine')
    ordered = sorted(rows, key=lambda row: row[column], reverse=direction == 'higher_is_better')
    result = {}
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][column] == ordered[start][column]:
            end += 1
        # Zero is best, one worst; ties have identical average percentile.
        percentile = ((start + end - 1) / 2) / max(1, len(ordered) - 1)
        for row in ordered[start:end]:
            result[row['input_sdf_index']] = percentile
        start = end
    return result


def select(rows, policy, directions):
    validate(policy)
    if set(directions) != {'carsiscore', 'rtmscore'} or any(value not in DIRECTIONS for value in directions.values()):
        raise ValueError('Both engine score directions are mandatory')
    if not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError('Selection requires a bounded verified core candidate table')
    indexes, compounds, eligible, audit = set(), set(), [], []
    for row in rows:
        index, compound = row.get('input_sdf_index'), row.get('compound_id')
        if type(index) is not int or index < 0 or not isinstance(compound, str) or not compound.strip():
            raise ValueError('Invalid molecular identity')
        if index in indexes or compound in compounds:
            raise ValueError('Duplicate molecular identity')
        indexes.add(index)
        compounds.add(compound)
        reason = None
        if row.get('carsidock_status') != 'SUCCESS' or row.get('rtmscore_status') != 'SUCCESS':
            reason = 'upstream_engine_failed_or_missing'
        elif not finite(row.get('carsiscore')) or not finite(row.get('rtmscore')):
            reason = 'nonfinite_or_missing_score'
        audit.append({'input_sdf_index': index, 'compound_id': compound, 'eligible': reason is None,
                      'excluded_reason': reason, 'selected': False, 'selection_channel': None})
        if reason is None:
            eligible.append(row)
    if policy['diversity_count'] and any(not isinstance(row.get('scaffold_smiles'), str) or not row['scaffold_smiles'] for row in eligible):
        raise ValueError('Diversity selection requires recorded scaffold identities; do not invent them')
    carsi = ranks(eligible, 'carsiscore', directions['carsiscore'])
    rtms = ranks(eligible, 'rtmscore', directions['rtmscore'])
    evidence = {}
    for row in eligible:
        index = row['input_sdf_index']
        evidence[index] = {**row, 'carsidock_rank_fraction': carsi[index], 'rtmscore_rank_fraction': rtms[index],
                           'rank_disagreement': abs(carsi[index] - rtms[index]),
                           'best_rank_fraction': min(carsi[index], rtms[index]),
                           'worst_rank_fraction': max(carsi[index], rtms[index])}
    cap = min(policy['max_candidates'], math.floor(len(eligible) * policy['max_fraction']))
    chosen, used, scaffolds = [], set(), set()

    def take(pool, channel, quota):
        count = 0
        for row in pool:
            if len(chosen) >= cap or count >= quota:
                break
            if row['input_sdf_index'] in used:
                continue
            if channel == 'diversity_sentinel' and row['scaffold_smiles'] in scaffolds:
                continue
            used.add(row['input_sdf_index'])
            if row.get('scaffold_smiles'):
                scaffolds.add(row['scaffold_smiles'])
            chosen.append({**row, 'selection_channel': channel})
            count += 1

    stable = lambda row: (row['compound_id'], row['input_sdf_index'])
    leading = [row for row in evidence.values() if row['best_rank_fraction'] <= policy['top_fraction']]
    uncertain = sorted((row for row in leading if row['rank_disagreement'] >= policy['disagreement_threshold']),
                       key=lambda row: (-row['rank_disagreement'], row['best_rank_fraction'], *stable(row)))
    take(uncertain, 'rank_disagreement_review', policy['uncertainty_count'])
    consensus = sorted((row for row in leading if row['worst_rank_fraction'] <= policy['top_fraction']),
                       key=lambda row: (row['worst_rank_fraction'], row['best_rank_fraction'], *stable(row)))
    take(consensus, 'consensus_sentinel', policy['consensus_count'])
    diverse = sorted(leading, key=lambda row: (row['best_rank_fraction'], row['worst_rank_fraction'], *stable(row)))
    take(diverse, 'diversity_sentinel', policy['diversity_count'])
    channels = {row['input_sdf_index']: row['selection_channel'] for row in chosen}
    for row in audit:
        item = evidence.get(row['input_sdf_index'])
        if item:
            for key in ('carsidock_rank_fraction', 'rtmscore_rank_fraction', 'rank_disagreement'):
                row[key] = item[key]
        row['selected'] = row['input_sdf_index'] in used
        row['selection_channel'] = channels.get(row['input_sdf_index'])
    selected = [{key: row[key] for key in ('input_sdf_index', 'compound_id', 'selection_channel',
                'carsidock_rank_fraction', 'rtmscore_rank_fraction', 'rank_disagreement')} for row in chosen]
    return {'algorithm': VERSION, 'policy': policy, 'directions': directions,
            'input_count': len(rows), 'eligible_count': len(eligible), 'excluded_count': len(rows) - len(eligible),
            'effective_cap': cap, 'selected_count': len(selected), 'selected': selected,
            'audit': sorted(audit, key=lambda row: (row['compound_id'], row['input_sdf_index'])),
            'status': 'SUBSET_PROPOSED' if selected else 'NO_CANDIDATES_MEET_POLICY',
            'calibrated_uncertainty': False, 'gpu_submitted': False,
            'limitations': ['Rank disagreement is a review heuristic, not an uncertainty probability or potency estimate.',
                           'No ensemble variance, calibrated error model or reactive-geometry uncertainty is available here.',
                           'Consensus/diversity entries are explicitly labelled controls, not uncertain predictions.',
                           'Unused channel budgets are not refilled; no whole-pool fallback.',
                           'AF3/CovDock require a separate immutable subset and resource approval.']}
