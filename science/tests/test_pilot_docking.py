import json
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

from covscreen.jobs import sha256_file, JobSubmissionRequest
from covscreen.execution import JobManager
from covscreen.pilot_docking import prepare_docking
from test_result_verification import fixture, refresh
from test_docking_reference import asset
from test_library_site_handoff import selected


def prepared(root):
    reference = asset(root)
    binding, selection_result = selected(root, receptor_bytes=Path(reference['pdb']).read_bytes())
    selection = selection_result['result']
    registry, result, output = fixture(root)
    artifacts = registry.job_dir(result.run_id) / 'artifacts'
    shutil.move(str(output), artifacts)
    frame = pd.read_parquet(artifacts / 'stage2_candidates.parquet')
    frame['input_sdf_index'] = [0]
    frame.to_parquet(artifacts / 'stage2_candidates.parquet', index=False)
    frame.to_csv(artifacts / 'stage2_candidates.csv', index=False)
    pilot = root / 'pilot.json'
    pilot.write_text(json.dumps({'selection': binding, 'target': selection['target'],
                                'structure': selection['structure'], 'site': selection['selected_sites'][0]}))
    spec = registry.load_spec(result.run_id)
    spec.parameters.update(pilot_manifest='pilot.json', target_config='target.yaml')
    spec.input_sha256['pilot.json'] = sha256_file(pilot)
    spec.output_directory = str(artifacts)
    spec.expected_output_paths = [str(artifacts)]
    registry.write_spec(spec)
    refresh(registry, result, artifacts)
    repo = root / 'engine';repo.mkdir()
    checkpoint = repo / 'fixture.ckpt';checkpoint.write_text('planning fixture; never executed')
    config = {'library_docking_references': [reference], 'runtime': {'python': sys.executable},
              'carsidock': {'repo': str(repo), 'checkpoint': str(checkpoint)}}
    (root / 'configs').mkdir()
    (root / 'configs/local.inspire.yaml').write_text(yaml.safe_dump(config))
    (root / 'configs/library_docking_references.json').write_text(json.dumps({'schema': 'library-docking-references-v1', 'references': [reference]}))
    return registry, result, config


def test_pilot_docking_plan_is_bound_and_does_not_submit(tmp_path):
    registry, result, _ = prepared(tmp_path)
    before = sorted(p.name for p in registry.root.iterdir())
    plan = prepare_docking(tmp_path, {'prescreen_run_id': result.run_id})
    assert plan['submitted'] is False
    assert plan['calculation_submitted'] is False
    assert plan['approval_required'] is True
    assert plan['structure_coverage']['observed_author_ranges'] == [[773, 773]]
    assert plan['structure_coverage']['full_length_coverage_verified'] is False
    assert plan['route_plan']['terminal_stage'] == 'rtmscore'
    request = plan['next_request']
    assert request['parent_run_id'] == result.run_id
    assert request['parameters']['covres'] == 'A:CYS:773'
    assert request['parameters']['docking_binding'] in plan['execution_plan']['input_sha256']
    assert sorted(p.name for p in registry.root.iterdir()) == before
    changed = JobSubmissionRequest.model_validate(request)
    changed.parameters['covres'] = 'A:CYS:797'
    with pytest.raises(ValueError, match='author numbering'):
        JobManager(tmp_path).build_spec(changed)


def test_missing_registered_reference_blocks_without_guessing(tmp_path):
    registry, result, config = prepared(tmp_path)
    (tmp_path / 'configs/library_docking_references.json').write_text(json.dumps({'schema': 'library-docking-references-v1', 'references': []}))
    (tmp_path / 'configs/local.inspire.yaml').write_text(yaml.safe_dump(config))
    plan = prepare_docking(tmp_path, {'prescreen_run_id': result.run_id})
    assert plan['status'] == 'BLOCKED'
    assert plan['calculation_submitted'] is False
    assert not (tmp_path / 'state/pilot_docking').exists()
