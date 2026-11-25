"""Real storage/subprocess recovery; injected failures are not scientific evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from covscreen.ligand_workflow import ENTRY, EVIDENCE, directory, navigate, step
from covscreen.target_hypothesis import collect, folder as evidence_folder, run_collection, save
from covscreen.workflow_protocol import begin_operation
from covscreen.workflow_store import load
from test_ligand_workflow import target_artifact


def started(root, site=None):
    return navigate(root, "start", request_id="recover", target="EGFR", taxon=9606, site=site)


def pending(root, current, action, recovery=None):
    out = directory(root, current["workflow"]["workflow_id"])
    state = load(out)
    begin_operation(out, state, EVIDENCE, action, recovery=recovery)
    return state


def reconcile(root, state):
    return navigate(root, "reconcile", workflow_id=state["workflow_id"], expected_revision=state["revision"])


def reservation():
    return {"kind": "ligand-evidence-collection-v1", "evidence_id": "hyp-" + "d" * 24,
            "target": {"target": "EGFR", "site": "C797", "taxon": 9606}}


def env():
    return {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}


def test_abrupt_process_exit_recovers_local_target_step_without_adopting_partial_output(tmp_path, target_artifact):
    initial = started(tmp_path)
    out = directory(tmp_path, initial["workflow"]["workflow_id"])
    # Exit a real process after the durable operation boundary, without an
    # exception handler or successful tool response.
    script = """
import os,sys
from pathlib import Path
from covscreen.workflow_store import load
from covscreen.workflow_protocol import begin_operation
out=Path(sys.argv[1]);state=load(out)
begin_operation(out,state,'covalent_ligand_evidence','resolve_target')
(out/'partial-result.json').write_text('{"not_committed":true}')
os._exit(73)
"""
    result = subprocess.run([sys.executable, "-c", script, str(out)], env=env(), timeout=20)
    assert result.returncode == 73
    state = load(out)
    observed = navigate(tmp_path, "resume", workflow_id=state["workflow_id"])
    assert "reconcile" in observed["workflow"]["tools"][ENTRY]["actions"]
    assert "reason" not in observed["workflow"]["tools"][ENTRY]["fields"]
    assert EVIDENCE not in observed["workflow"]["tools"]
    restored = reconcile(tmp_path, state)
    assert restored["target_identity"] is None and restored["pending"] is None
    assert restored["result"]["partial_artifacts_adopted"] is False
    assert restored["result"]["automatic_retry"] is False
    assert (out/'partial-result.json').read_text() == '{"not_committed":true}'
    with pytest.raises(ValueError, match="STALE_WORKFLOW"):
        reconcile(tmp_path, state)
    resolved = step(tmp_path, EVIDENCE, workflow_id=state["workflow_id"],
        expected_revision=restored["workflow"]["revision"], action="resolve_target", **target_artifact)
    assert resolved["target_identity"] == target_artifact
    assert resolved["workflow"]["workflow_id"] == initial["workflow"]["workflow_id"]
    assert len(list((tmp_path/'state/ligand-workflows').glob('*/state.json'))) == 1
    assert not (tmp_path/'state/jobs').exists()


def test_missing_collection_receipt_never_restarts(tmp_path):
    state = pending(tmp_path, started(tmp_path, 'C797'), 'collect', reservation())
    original = (directory(tmp_path, state['workflow_id'])/'state.json').read_bytes()
    for _ in range(2):
        result = reconcile(tmp_path, state)
        assert result['result']['operation_state'] == 'OUTCOME_UNKNOWN'
        assert result['pending'] == state['pending']
        assert (directory(tmp_path, state['workflow_id'])/'state.json').read_bytes() == original
    assert not (tmp_path/'results/target-hypotheses').exists()
    with pytest.raises(ValueError, match='unavailable'):
        step(tmp_path, EVIDENCE, workflow_id=state['workflow_id'], expected_revision=state['revision'], action='collect')


def test_real_worker_launch_failure_has_reserved_receipt_and_can_be_reconsidered(tmp_path):
    script = """
import sys
from pathlib import Path
from covscreen.ligand_workflow import navigate,step,EVIDENCE
root=Path(sys.argv[1]);result=navigate(root,'start',request_id='launch-failure',target='EGFR',taxon=9606,site='C797')
sys.executable=str(root/'missing-python')
try:step(root,EVIDENCE,workflow_id=result['workflow']['workflow_id'],expected_revision=result['workflow']['revision'],action='collect')
except FileNotFoundError:pass
else:raise AssertionError('Expected a real operating-system launch failure')
"""
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], env=env(), timeout=20, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    out = next((tmp_path/'state/ligand-workflows').iterdir())
    state = load(out)
    identifier = state['pending']['recovery']['evidence_id']
    location = evidence_folder(tmp_path, identifier)
    assert json.loads((location/'status.json').read_text())['reason'] == 'worker_launch_failed'
    assert not (location/'worker.json').exists()
    recovered = reconcile(tmp_path, state)
    assert recovered['workflow']['stage'] == 'EVIDENCE_FAILED'
    assert recovered['evidence_id'] == identifier
    assert recovered['assessment']['state'] == 'not_evaluated'
    assert EVIDENCE not in recovered['workflow']['tools']
    reopened = navigate(tmp_path, 'reconsider', workflow_id=state['workflow_id'],
        expected_revision=recovered['workflow']['revision'], reason='Repair the unavailable interpreter before a new collection')
    assert reopened['workflow']['stage'] == 'INTAKE' and reopened['evidence_id'] is None
    assert any(e.get('evidence_id') == identifier for e in load(out)['events'])
    assert len(list((tmp_path/'results/target-hypotheses').iterdir())) == 1


def test_reconcile_observes_existing_live_worker_without_launching_another(tmp_path):
    recovery = reservation()
    state = pending(tmp_path, started(tmp_path, 'C797'), 'collect', recovery)
    location = evidence_folder(tmp_path, recovery['evidence_id']);location.mkdir(parents=True)
    save(location/'request.json', recovery['target'])
    # A controlled real process supplies liveness, not chemical evidence.
    worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        save(location/'worker.json', {'pid': worker.pid})
        result = reconcile(tmp_path, state)
        assert result['workflow']['stage'] == 'EVIDENCE_PENDING'
        assert result['result']['collection_state'] == 'RUNNING'
        assert worker.poll() is None
        assert json.loads((location/'worker.json').read_text()) == {'pid': worker.pid}
        with pytest.raises(FileExistsError):
            collect(tmp_path, **recovery['target'], identifier=recovery['evidence_id'])
    finally:
        worker.terminate();worker.wait(timeout=10)


@pytest.mark.parametrize('success', [True, False])
def test_finished_real_collection_is_recovered_with_exact_identity(tmp_path, success):
    recovery = reservation()
    state = pending(tmp_path, started(tmp_path, 'C797'), 'collect', recovery)
    location = evidence_folder(tmp_path, recovery['evidence_id']);location.mkdir(parents=True)
    save(location/'request.json', recovery['target'])
    collector = tmp_path/'bounded-collector.py'
    collector.write_text("""
import json,sys
from pathlib import Path
out=Path(sys.argv[sys.argv.index('--output')+1])
(out/'evidence.json').write_text(json.dumps({'target':'EGFR','site':'C797','taxon':9606,'literature':[]}))
""" if success else 'raise SystemExit(9)\n')
    run_collection(tmp_path, recovery['evidence_id'], collector=collector)
    before = {p.name: p.read_bytes() for p in location.iterdir() if p.is_file()}
    result = reconcile(tmp_path, state)
    assert result['workflow']['stage'] == ('PARENT_REVIEW' if success else 'EVIDENCE_FAILED')
    assert result['evidence_id'] == recovery['evidence_id']
    assert result['result']['automatic_retry'] is False
    assert {p.name: p.read_bytes() for p in location.iterdir() if p.is_file()} == before


def test_collection_receipt_for_another_scope_is_not_adopted(tmp_path):
    recovery = reservation()
    state = pending(tmp_path, started(tmp_path, 'C797'), 'collect', recovery)
    location = evidence_folder(tmp_path, recovery['evidence_id']);location.mkdir(parents=True)
    save(location/'request.json', {**recovery['target'], 'target': 'BTK'})
    save(location/'status.json', {'state': 'FAILED'})
    with pytest.raises(ValueError, match='another target'):
        reconcile(tmp_path, state)
    assert load(directory(tmp_path, state['workflow_id']))['pending'] == state['pending']
