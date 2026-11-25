"""Direct graph input through the shared planner and native LO registration.

Historical receptor coordinates and review judgments are explicit software
fixtures. No VS record, scientific worker or vendor computation is created.
"""
import json

import pytest

from covscreen.execution import JobManager
from covscreen.ligand_design import generate, write
from covscreen.ligand_workflow import DESIGN, directory, navigate
from covscreen.mcp.server import dispatch
from covscreen.instance_lo_claim import inspect
from test_covdock_execution_workflow import (
    design, graph_design, bound, planned_receptor, isolated, completed, reviewed, ready, args, approval)


@pytest.fixture
def inputs(tmp_path, graph_design, monkeypatch, requested_target):
    # Existing ring-only fixture isolates execution integration; production
    # protected-pharmacophore generation is not weakened by this test.
    design = graph_design
    generated = generate(tmp_path, design)
    assert generated['candidate_count'] > 0
    initial = navigate(tmp_path, 'start', request_id='direct-lo-native-plan',
                       target=requested_target, site='C797', taxon=9606)
    identifier = initial['workflow']['workflow_id']
    path = directory(tmp_path, identifier) / 'state.json'
    state = json.loads(path.read_text())
    state.update(stage='NEEDS_POSE_EVALUATION', design_id=design, evidence_id='hyp-' + 'b'*24)
    write(path, state)
    case = tmp_path, identifier, path
    config = tmp_path / 'configs/local.inspire.yaml'
    config.parent.mkdir()
    write(config, {'runtime': {'enable_ligand_lo_instance': True}})
    key = tmp_path / 'isolated-test-only.key'
    key.write_text('software-test-key-not-a-production-credential' * 2)
    key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    dispatch(tmp_path, DESIGN, args(case, 'prepare_docking_input'))
    dispatch(tmp_path, DESIGN, args(case, 'prepare_direct_lo_target'))
    return case


@pytest.mark.parametrize('design,requested_target', [('EGFR', 'EGFR'), ('P00533', 'P00533')],
                         indirect=['design'])
def test_direct_native_plan_registers_exact_instance_recipe_without_vs(ready, requested_target):
    case, launches, _, _, receptor_run = ready
    root, identifier, path = case
    state = json.loads(path.read_text())
    assert state['target'] == {'target': requested_target, 'site': 'C797', 'taxon': 9606}
    assert not any(state.get(k) for k in ('vs_input', 'vs_execution', 'pose_execution', 'af3_execution'))
    count = state['refinement_input']['input_count']
    plan = state['refinement_input']['covdock_plan']['spec']
    assert plan['provenance']['worker_backend'] == 'external-instance'
    assert plan['parameters']['execution_instance'] == 'asgbie3'
    from covscreen.covdock_gate import LIGAND_ALGORITHM_VERSION
    assert plan['parameters']['gate_algorithm_version'] == LIGAND_ALGORITHM_VERSION
    assert plan['resource_budget']['max_candidates'] == count
    assert plan['resource_budget']['cpu_count'] == min(count, 4)
    assert plan['resource_budget']['max_runtime_minutes'] == 240
    with pytest.raises(ValueError, match='approval'):
        dispatch(root, DESIGN, {**args(case, 'start_covdock'), 'human_approved': True})
    proof = approval(case)
    result = dispatch(root, DESIGN, proof)
    run = result['covdock_execution']['run_id']
    observed = inspect(root, run, 'asgbie3')
    assert observed['state'] == 'QUEUED' and not observed['worker_claimed']
    assert result['result']['execution_instance'] == 'asgbie3'
    assert run != receptor_run
    assert len(launches) == 1  # Only the isolated no-op receptor worker.
    assert len(list(root.glob('state/jobs/*/spec.json'))) == 2
    with pytest.raises(ValueError):
        dispatch(root, DESIGN, proof)
    resumed = navigate(root, 'resume', workflow_id=identifier)
    assert resumed['covdock_execution']['run_id'] == run
    spec = JobManager(root).registry.load_spec(run)
    assert spec.parameters['input_row_count'] == count
    assert '--prepare-receptor' in spec.command
