"""Exercise entry scripts, not only the isolated environment helper."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_preparation_entry_isolates_native_child(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (tmp_path / 'src').symlink_to(ROOT / 'src', target_is_directory=True)
    shutil.copy2(ROOT / 'scripts/prepare_af3_covalent_batch.py', scripts)
    # A synthetic child checks process isolation, not chemical correctness.
    (scripts / 'prepare_af3_covalent_input.py').write_text('''
import argparse, importlib.util, json
from pathlib import Path
import numpy
assert importlib.util.find_spec('orchestrator_only_marker') is None
p=argparse.ArgumentParser()
p.add_argument('--output-dir'); p.add_argument('--job-name')
a,_=p.parse_known_args()
d=Path(a.output_dir)
(d/'LIG.cif').write_text('synthetic test fixture')
(d/(a.job_name+'.json')).write_text('{}')
(d/(a.job_name+'.manifest.json')).write_text(json.dumps({
 'input_json':str(d/(a.job_name+'.json')), 'job_name':a.job_name,
 'status':'SUCCESS'}))
''')
    poison = tmp_path / 'orchestrator-packages'
    poison.mkdir()
    (poison / 'orchestrator_only_marker.py').write_text('MARKER = True')
    table = tmp_path / 'candidates.csv'
    table.write_text('compound_id,standardized_smiles,input_sdf_index\n007,C,0\n')
    source = tmp_path / 'source.json'
    source.write_text(json.dumps({'sequences': [
        {'protein': {'id': 'A', 'sequence': 'A'*87+'C'}}]}))
    environment = dict(os.environ, PYTHONPATH=f'{ROOT / "src"}:{poison}')
    result = subprocess.run([sys.executable, str(scripts / 'prepare_af3_covalent_batch.py'),
        '--af3-python', sys.executable, '--library-table', str(table),
        '--source-data-json', str(source), '--all-valid', '--fail-fast',
        '--output-dir', str(tmp_path / 'output')], env=environment,
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    rows = json.loads((tmp_path / 'output/batch_manifest.json').read_text())
    assert len(rows) == 1 and rows[0]['status'] == 'SUCCESS'
    assert rows[0]['compound_id'] == '007'


def test_inference_entry_uses_clean_environment(tmp_path, monkeypatch):
    script = ROOT / 'scripts/run_af3_job.py'
    spec = importlib.util.spec_from_file_location('af3_runner_boundary_test', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runner = tmp_path / 'runner.py'
    runner.write_text('''import importlib.util, numpy
assert importlib.util.find_spec('orchestrator_only_marker') is None
''')
    class Adapter:
        name = 'synthetic-runtime-only'
        def __init__(self, **kwargs):
            self.python, self.runner = Path(kwargs['python']), Path(kwargs['runner'])
        def check_availability(self):
            return {'ready': True}
        def build_command(self, **kwargs):
            return [str(self.python), str(self.runner)]
        def parse_output(self, output):
            return {}
        def validate_output(self, parsed):
            return True
    monkeypatch.setattr(module, 'AlphaFold3Adapter', Adapter)
    poison = tmp_path / 'orchestrator-packages'
    poison.mkdir()
    (poison / 'orchestrator_only_marker.py').write_text('MARKER=True')
    monkeypatch.setenv('PYTHONPATH', str(poison))
    source = tmp_path / 'input.json'
    source.write_text('{}')
    monkeypatch.setattr(sys, 'argv', [str(script), '--json-path', str(source),
        '--output-dir', str(tmp_path / 'output'), '--runner', str(runner),
        '--af3-python', sys.executable, '--model-dir', str(tmp_path),
        '--database-dir', str(tmp_path)])
    assert module.main() == 0
    manifest = json.loads((tmp_path / 'output.run_manifest.json').read_text())
    assert manifest['return_code'] == 0
