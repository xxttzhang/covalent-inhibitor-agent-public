"""Replay registered LO measurements with their immutable original reader."""
import json
from pathlib import Path
import subprocess
import sys


def evaluate(root, spec):
    from covscreen.native_approval import verify_spec
    from covscreen.source_release import historical_release
    verify_spec(spec)
    reference = spec.provenance['source_release']
    release = historical_release(reference)
    # The prereaction input seals the original review hash, including reader paths.
    program = '''
import json, sys
from pathlib import Path
release, root, run = sys.argv[1:]
sys.path.insert(0, str(Path(release) / 'science/src'))
sys.path.insert(1, str(Path(release) / 'routes/de-novo/src'))
from covscreen.execution import JobManager
from covscreen.native_approval import verify_spec
from covscreen.source_release import verify_source
from covscreen.ligand_covdock_evidence import evaluate_outputs
root = Path(root)
manager = JobManager(root)
spec, result = manager.registry.load_spec(run), manager.registry.load_result(run)
verify_spec(spec)
verify_source(spec)
if spec.route != 'de-novo' or spec.engine != 'schrodinger-covdock-lo':
    raise ValueError('Historical reader requires a registered De novo LO task')
state = {'covdock_execution': {'run_id': run},
    'refinement_input': {'artifact_id': 'denovo-batch-lo-' + spec.parameters['denovo_batch_lo_sha256']},
    'target': {'target_id': 'EGFR', 'site_id': 'EGFR_C797'}}
print(json.dumps(evaluate_outputs(root, state, manager, spec, result, route='de-novo'), allow_nan=False))
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, release['root'],
        str(Path(root).resolve(strict=True)), spec.run_id], capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise ValueError('Historical LO readback failed: ' + result.stderr[-1600:])
    review = json.loads(result.stdout)
    if review.get('schema') != 'denovo-covdock-measurement-review-v1' or review.get('run_id') != spec.run_id:
        raise ValueError('Historical LO reader returned a different task')
    historical_release(reference)
    return review
