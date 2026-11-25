"""Evidence-ranked De novo candidates; no combined cross-engine activity score."""
import hashlib
import math
from pathlib import Path
from .batch_lo_input import encoded


def ranking(lo, prereaction):
    states = {row['candidate_id']: row for row in prereaction['states'] if not row['control']}
    if len(states) != sum(not row['control'] for row in prereaction['states']):
        raise ValueError('Duplicate prereaction candidate')
    if not set(states).issubset({row['compound_id'] for row in lo['candidates']}):
        raise ValueError('Prereaction evidence belongs to foreign LO candidates')
    rows = []
    for candidate in lo['candidates']:
        name = candidate['compound_id']
        state = states.get(name)
        gate = candidate['gate']
        physical = candidate['engine_status'] == 'SUCCESS' and gate['physical_validity'] == 'PASS' and gate['physical_evidence_complete'] is True
        if state is None:
            pre = 'NOT_EVALUATED'
        elif state['identity_verified_count'] == 0:
            pre = 'EVIDENCE_INSUFFICIENT'
        elif state['eligible_pose_count'] > 0:
            pre = 'EXPLORATORY_GEOMETRY_PRESENT'
        else:
            pre = 'NO_QUALIFYING_POSE_OBSERVED'
        tier = (0 if pre == 'EXPLORATORY_GEOMETRY_PRESENT' else 1 if pre in {'NOT_EVALUATED', 'EVIDENCE_INSUFFICIENT'} else 2) if physical else 3
        score = candidate.get('covdock_score')
        if score is not None and (type(score) not in (int, float) or not math.isfinite(score)):
            raise ValueError('Nonfinite LO score in final report')
        rows.append({'candidate_id': name, 'input_index': candidate['input_sdf_index'],
            'evidence_tier': tier, 'engine_status': candidate['engine_status'], 'lo_physical_gate': gate,
            'lo_score': score, 'lo_measurements': candidate.get('measurements'),
            'prereaction_state': pre, 'prereaction': state, 'validated_inhibitor': False})
    if len({row['candidate_id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate LO candidate in report')
    rows.sort(key=lambda row: (row['evidence_tier'], float('inf') if row['lo_score'] is None else row['lo_score'], row['candidate_id']))
    return {'schema': 'denovo-candidate-ranking-v1', 'candidates': [{**row, 'rank': index+1} for index, row in enumerate(rows)],
        'candidate_count': len(rows), 'controls': [row for row in prereaction['states'] if row['control']],
        'ranking_rule': 'LO physical PASS plus observed prereaction geometry first; incomplete prereaction evidence next; no observed qualifying pose next; LO physical checks not passed last. Within tier: LO score ascending, then candidate ID.',
        'score_combination': 'NONE', 'scientific_acceptance': False, 'md_ready': False,
        'limitations': ['Ranking is computational prioritization, not validated potency, reactivity or selectivity.',
            'Unmeasured prereaction candidates are not failures; absence of a sampled qualifying pose is not proof of inactivity.',
            'Lowest-score prereaction pose and best qualifying pose are both retained; a favorable alternative is not hidden as the top-scoring result.',
            'Provisional geometry does not establish full physical validity or a reaction rate.']}


def report(root, source_digest, manifest_digest):
    from .batch_prereaction_input import load
    from .batch_lo import observe as lo_observe
    from .batch_glide import observe as glide_observe
    root = Path(root).resolve(strict=True)
    inputs, _ = load(root, source_digest, manifest_digest)
    lo = lo_observe(root, inputs['source']['lo_manifest_sha256'], evaluate=True)
    glide = glide_observe(root, source_digest, manifest_digest, evaluate=True)
    result = {**ranking(lo['evaluation'], glide['evaluation']), 'task_id': inputs['task_id'],
        'workflow_id': inputs['workflow_id'], 'lo_run_id': lo['run_id'], 'glide_run_id': glide['run_id'],
        'lo_evaluation_artifact': lo['evaluation_artifact'], 'glide_output_sha256': glide['evaluation']['output_sha256'],
        'source_sha256': source_digest, 'manifest_sha256': manifest_digest, 'compute_submitted': False}
    raw = encoded(result)
    digest = hashlib.sha256(raw).hexdigest()
    folder = root / 'results/de-novo-batch-reports' / digest
    path = folder / 'report.json'
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Final report cannot traverse symlinks')
    folder.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open('xb') as stream:
            stream.write(raw)
    if path.read_bytes() != raw:
        raise ValueError('Frozen final report changed')
    return {**result, 'report_artifact': {'path': str(path.relative_to(root)), 'sha256': digest}}
