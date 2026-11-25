"""Replay the benchmark's exploratory prereaction identity and geometry checks."""
import math
from rdkit import Chem

POLICY = {'version': 'denovo-independent-glide-prereaction-v1', 'sg_beta_window_A': [2.8, 4.0],
    'sg_beta_alpha_angle_degrees': [80, 140], 'intraligand_vdw_scale': 0.75,
    'max_intraligand_clashing_fraction': 0.2, 'severe_nonbonded_distance_A': 1.2,
    'scope': 'exploratory approach geometry; not kinetics, affinity, activity or complete physical validation'}


def measure(pose, original, expected, sulfur):
    if pose is None or not pose.HasProp('_Name') or pose.GetProp('_Name') != expected['candidate_id']:
        raise ValueError('Prereaction pose identity differs')
    if pose.GetNumAtoms() != original.GetNumAtoms() or pose.GetNumBonds() != original.GetNumBonds():
        raise ValueError('Prereaction graph size differs')
    matches = pose.GetSubstructMatches(original, useChirality=True, uniquify=False, maxMatches=4096)
    if not matches or len(matches) >= 4096:
        raise ValueError('Prereaction graph/stereochemistry mismatch or mapping bound reached')
    mappings = {(m[expected['beta_index_0based']], m[expected['alpha_index_0based']]) for m in matches}
    if len(mappings) != 1:
        raise ValueError('Ambiguous prereaction reaction-atom mapping')
    beta, alpha = next(iter(mappings))
    xyz = pose.GetConformer().GetPositions()
    if not all(math.isfinite(float(x)) for point in [*xyz, sulfur] for x in point):
        raise ValueError('Nonfinite prereaction coordinates')
    a = [float(xyz[alpha][i] - xyz[beta][i]) for i in range(3)]
    b = [float(sulfur[i] - xyz[beta][i]) for i in range(3)]
    denominator = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    if not denominator:
        raise ValueError('Degenerate prereaction angle')
    angle = math.degrees(math.acos(max(-1., min(1., sum(x*y for x, y in zip(a, b)) / denominator))))
    distance = math.dist(sulfur, xyz[beta])
    score = float(pose.GetProp('r_i_glide_gscore'))
    if not math.isfinite(score):
        raise ValueError('Nonfinite Glide score')
    heavy = [atom.GetIdx() for atom in pose.GetAtoms() if atom.GetAtomicNum() > 1]
    affected, severe = set(), []
    table = Chem.GetPeriodicTable()
    for offset, i in enumerate(heavy):
        direct = {a.GetIdx() for a in pose.GetAtomWithIdx(i).GetNeighbors()}
        excluded = direct | {i}
        for j in direct:
            excluded.update(a.GetIdx() for a in pose.GetAtomWithIdx(j).GetNeighbors())
        for j in heavy[offset+1:]:
            if j in excluded:
                continue
            d = math.dist(xyz[i], xyz[j])
            if d < POLICY['severe_nonbonded_distance_A']:
                severe.append([i, j, d])
            if d < POLICY['intraligand_vdw_scale'] * sum(table.GetRvdw(pose.GetAtomWithIdx(k).GetAtomicNum()) for k in (i, j)):
                affected.update((i, j))
    fraction = len(affected) / len(heavy)
    geometry = POLICY['sg_beta_window_A'][0] <= distance <= POLICY['sg_beta_window_A'][1] and POLICY['sg_beta_alpha_angle_degrees'][0] <= angle <= POLICY['sg_beta_alpha_angle_degrees'][1]
    intra = not severe and fraction <= POLICY['max_intraligand_clashing_fraction']
    return {'identity_verified': True, 'score': score, 'sg_beta_A': distance,
        'sg_beta_alpha_angle_degrees': angle, 'geometry_passed': geometry,
        'intraligand': {'clashing_fraction': fraction, 'severe_pairs': severe, 'passed': intra},
        'structurally_eligible': geometry and intra}


def summarize(rows):
    verified = [r for r in rows if r.get('identity_verified')]
    eligible = [r for r in verified if r.get('structurally_eligible')]
    key = lambda row: (row['score'], row['raw_file'], row['pose_index'])
    return {'pose_count': len(rows), 'identity_verified_count': len(verified),
        'eligible_pose_count': len(eligible), 'best_score_pose': min(verified, key=key) if verified else None,
        'best_geometry_pose': min(eligible, key=key) if eligible else None}
