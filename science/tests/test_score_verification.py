import json

import pandas as pd
import pytest
from rdkit import Chem

from covscreen.jobs import ArtifactRegistry, JobSpec, ResourceBudget
from covscreen.score_verification import verify_scores


def setup(root, carsi):
    source = root / 'input.parquet'
    pd.DataFrame({'input_sdf_index': [0, 1], 'compound_id': ['a', 'b'],
                  'engine_status': ['SUCCESS', 'FAILED']}).to_parquet(source)
    output = root / 'out';output.mkdir()
    pose = output / 'poses.sdf'
    mol = Chem.MolFromSmiles('CC')
    mol.AddConformer(Chem.Conformer(2))
    with Chem.SDWriter(str(pose)) as writer:
        writer.write(mol)
    frame = pd.DataFrame({'input_sdf_index': [0, 1], 'compound_id': ['a', 'b'],
        'engine_status': ['SUCCESS', 'FAILED'], 'raw_score': [1.2, None],
        'score_direction': ['lower_is_better' if carsi else 'higher_is_better'] * 2,
        'pose_file': [str(pose), None], 'selected_pose_record_index': [0, None]})
    stem = 'carsidock_scores' if carsi else 'rtmscore'
    table = output / (stem + '.parquet');frame.to_parquet(table)
    audit = ({'input_rows': 2, 'success_rows': 1, 'failed_rows': 1, 'identity_denominator_complete': True} if carsi else
             {'output_row_count': 2, 'scored_row_count': 1, 'failed_row_count': 1, 'status': 'SUCCESS', 'score_direction': 'higher_is_better'})
    manifest = output / (stem + ('_audit.json' if carsi else '.run_manifest.json'))
    manifest.write_text(json.dumps(audit))
    spec = JobSpec(run_id='score-test-001', request_id='score-test', project_id='test', route='library-first',
                   engine='carsidock-cov' if carsi else 'rtmscore', command=['not-executed'],
                   working_directory=str(root), output_directory=str(output),
                   parameters={'ligand_table' if carsi else 'carsidock_scores': str(source)},
                   resource_budget=ResourceBudget(max_runtime_minutes=1, max_candidates=2))
    registry = ArtifactRegistry(root)
    records = {p.resolve(): registry.register_file(spec.run_id, p, 'other') for p in output.iterdir()}
    return spec, records, table, manifest


@pytest.mark.parametrize('carsi', [True, False])
@pytest.mark.parametrize('damage', [None, 'missing_row', 'identity', 'nan', 'direction', 'audit'])
def test_scores_preserve_denominator_and_finite_success(tmp_path, carsi, damage):
    spec, records, table, manifest = setup(tmp_path, carsi)
    frame = pd.read_parquet(table)
    if damage == 'missing_row': frame = frame.iloc[:1]
    if damage == 'identity': frame.loc[0, 'compound_id'] = 'wrong'
    if damage == 'nan': frame.loc[0, 'raw_score'] = float('nan')
    if damage == 'direction': frame.loc[0, 'score_direction'] = 'unknown'
    if damage == 'audit': manifest.write_text('{}')
    frame.to_parquet(table)
    errors = verify_scores(tmp_path, spec, records)
    assert bool(errors) == (damage is not None)


def test_success_requires_registered_pose(tmp_path):
    spec, records, table, _ = setup(tmp_path, True)
    records.pop((tmp_path / 'out/poses.sdf').resolve())
    assert 'selected_pose_not_registered' in verify_scores(tmp_path, spec, records)
