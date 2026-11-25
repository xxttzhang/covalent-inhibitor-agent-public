import json

from covscreen.md_progress import MDProgress, read_equilibration_progress


def test_progress_roundtrip_does_not_grant_acceptance(tmp_path, capsys):
    progress = MDProgress(tmp_path, 'software-fixture', 250000)
    progress.report('integrating', 5000, time_ps=10)
    value = read_equilibration_progress(tmp_path, 'software-fixture')
    assert value['completed_steps'] == 5000
    assert value['scientific_acceptance'] is False
    assert 'not liveness' in value['meaning']
    assert json.loads(capsys.readouterr().out)['progress']['metrics']['time_ps'] == 10
    assert not (tmp_path / 'progress.next.json').exists()
    assert read_equilibration_progress(tmp_path, 'different-candidate')['unavailable']


def test_incomplete_or_out_of_range_progress_is_not_a_scientific_failure(tmp_path):
    assert read_equilibration_progress(tmp_path, 'test') is None
    (tmp_path / 'progress.json').write_text('{')
    assert read_equilibration_progress(tmp_path, 'test')['unavailable']
    MDProgress(tmp_path, 'test', 250000).report('integrating', 250001)
    assert read_equilibration_progress(tmp_path, 'test')['unavailable']
