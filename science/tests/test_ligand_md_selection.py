"""Selection decisions over artificial portable evidence; no scientific engine."""
import json
import sys

import pytest

from covscreen.jobs import sha256_file
from covscreen.ligand_md_selection import select, release, refresh, plan_graph, selection_status
from test_md_review_delivery import case, deliver


@pytest.fixture
def state(tmp_path, case):
    value, path = deliver(tmp_path, case)
    return {'workflow_id': 'ligand-flow-' + 'a' * 24, 'design_id': 'design-' + 'a' * 24,
        'covdock_execution': {'run_id': value['task_id'], 'evaluation_verification': 'verified',
            'md_handoff': {'path': str(path.relative_to(tmp_path)), 'manifest_sha256': sha256_file(path)}}}


def choose(root, state, **changes):
    return select(root, state, **{'compound_id': '007', 'input_sdf_index': 7,
                                 'reason': 'Artificial example: inspect this recorded pose', **changes})


def runtime(root, *, enabled=True):
    folder = root / 'runtime'
    folder.mkdir(exist_ok=True)
    for name in ('vendor', 'lib'):
        (folder / name).mkdir(exist_ok=True)
    (folder / 'license').write_text('INERT TEST FILE')
    launcher = folder / 'run'
    launcher.write_text("#!/bin/sh\nprintf '%s\\n' '{\"runtime\":\"schrodinger-python\",\"module\":\"schrodinger\"}'\n")
    launcher.chmod(0o700)
    config = root / 'configs/local.inspire.yaml'
    config.parent.mkdir(exist_ok=True)
    existing = json.loads(config.read_text()) if config.exists() else {}
    existing['adduct_parameterization'] = {'enabled': enabled, 'audit_python': sys.executable,
        **{key: str(folder / name) for key, name in [('launcher', 'run'), ('vendor_root', 'vendor'),
            ('license_file', 'license'), ('runtime_lib_dir', 'lib')]}}
    config.write_text(json.dumps(existing))


def test_selection_preserves_ids_reason_and_requires_explicit_release(tmp_path, state):
    assert selection_status(state)['state'] == 'no_active_selection'
    chosen = choose(tmp_path, state)
    assert selection_status(state)['state'] == 'selected'
    assert chosen['compound_id'] == '007' and chosen['input_sdf_index'] == 7
    assert chosen['md_admission']['state'] == 'not_evaluated'
    assert choose(tmp_path, state) == chosen
    with pytest.raises(ValueError, match='Release'):
        choose(tmp_path, state, reason='A changed reason')
    result = release(state, 'Reconsider this unexecuted choice')
    assert result['selection_status'] == 'deselected_and_archived'
    assert result['active_selection'] is None and not result['publication_performed']
    assert selection_status(state)['state'] == 'no_active_selection'
    assert selection_status(state)['archived_decision_count'] == 1
    assert state['covdock_execution']['md_selection_history'][0]['selection'] == chosen
    assert choose(tmp_path, state, reason='A changed reason')['reason'] == 'A changed reason'


@pytest.mark.parametrize('changes', [{'input_sdf_index': '7'}, {'input_sdf_index': True},
    {'compound_id': '7'}, {'reason': ''}, {'compound_id': 'failed', 'input_sdf_index': 9}])
def test_invalid_or_ineligible_choice_is_not_saved(tmp_path, state, changes):
    with pytest.raises(ValueError):
        choose(tmp_path, state, **changes)
    assert 'md_selection' not in state['covdock_execution']


def test_selection_tampering_withdraws_verified_status_then_recovers_same_decision(tmp_path, state):
    selected = choose(tmp_path, state)
    record = state['covdock_execution']
    path = tmp_path / 'state/candidate_pose_input' / selected['artifact_id'][21:] / 'covdock-source.maegz'
    original = path.read_bytes()
    path.write_bytes(b'changed')
    refresh(tmp_path, state)
    assert record['md_selection_verification'] == 'evidence_rejected'
    assert record['md_selection'] == selected
    assert 'md_graph_plan_error' not in record
    path.write_bytes(original)
    refresh(tmp_path, state)
    assert record['md_selection_verification'] == 'verified' and 'md_selection_error' not in record
    record['md_handoff']['manifest_sha256'] = 'f' * 64
    refresh(tmp_path, state)
    assert record['md_selection_verification'] == 'evidence_rejected'


def test_native_runtime_wait_recovers_to_plan_without_computation(tmp_path, state):
    choose(tmp_path, state)
    runtime(tmp_path, enabled=False)
    waiting = plan_graph(tmp_path, state)
    assert waiting['state'] == 'runtime_configuration_required'
    runtime(tmp_path)
    plan = plan_graph(tmp_path, state)
    assert plan['state'] == 'plan_compiled' and not plan['execution_authorized']
    assert plan['engine'] == 'library-first-md-adduct-admission'
    assert plan['discovery_route'] == 'ligand-first'
    assert plan['spec']['parameters']['discovery_origin']['route'] == 'ligand-first'
    assert plan['spec']['resource_budget']['gpu_count'] == 0
    assert plan_graph(tmp_path, state) == plan
    jobs = tmp_path / 'state/jobs'
    assert not jobs.exists() or not list(jobs.iterdir())
    runtime(tmp_path, enabled=False)
    waiting = plan_graph(tmp_path, state)
    assert waiting['spec'] == plan['spec']
    assert not waiting['native_graph_qc_completed']


def test_plan_source_failure_does_not_erase_selection_or_replace_frozen_plan(tmp_path, state):
    chosen = choose(tmp_path, state)
    runtime(tmp_path)
    plan = plan_graph(tmp_path, state)
    record = state['covdock_execution']
    frozen = tmp_path / plan['spec']['provenance']['config_path']
    original = frozen.read_bytes()
    frozen.write_bytes(original + b'\n')
    refresh(tmp_path, state)
    assert record['md_selection_verification'] == 'verified'
    assert record['md_selection'] == chosen
    assert record['md_graph_plan_error']
    assert record['md_graph_plan'] == plan
    frozen.write_bytes(original)
    refresh(tmp_path, state)
    assert 'md_graph_plan_error' not in record
    config = tmp_path / 'configs/local.inspire.yaml'
    changed = json.loads(config.read_text())
    old_launcher = tmp_path / 'runtime/run'
    replacement = tmp_path / 'runtime/replacement-run'
    replacement.write_bytes(old_launcher.read_bytes())
    replacement.chmod(0o700)
    changed['adduct_parameterization']['launcher'] = str(replacement)
    config.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='Frozen graph plan changed'):
        plan_graph(tmp_path, state)
    assert record['md_graph_plan'] == plan
