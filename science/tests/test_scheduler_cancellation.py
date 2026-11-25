"""Synthetic signed jobs only; no scheduler or scientific process is launched."""
import json

import pytest

from test_external_hpc import queued  # shared signed, isolated HPC fixture
from covscreen.external_hpc import inspect, reserve
from covscreen.jobs import ArtifactRegistry
from covscreen.native_approval import CURRENT, signed
from covscreen.scheduler_cancellation import scope, request, read_intent


def prepared(queued):
    root, run = queued
    reserve(root, run, inspect(root, run)['spec_sha256'])
    return ArtifactRegistry(root), run


def test_signed_request_preserves_active_job_and_all_original_evidence(queued):
    registry, run = prepared(queued)
    expected = scope(registry, run)
    folder = registry.job_dir(run)
    originals = {name: (folder / name).read_bytes()
                 for name in ('spec.json', 'result.json', 'external_hpc.json')}
    token = CURRENT.set(signed({'tool': 'covalent_cancel_job', 'scope': expected}))
    try:
        first = request(registry, run)
        second = request(registry, run)
    finally:
        CURRENT.reset(token)
    assert first == second == read_intent(registry, run)
    assert first['termination_confirmed'] is False
    assert registry.load_result(run).state == 'QUEUED'
    assert originals == {name: (folder / name).read_bytes() for name in originals}


@pytest.mark.parametrize('mutation', ['name', 'digest', 'missing', 'symlink'])
def test_unbound_scheduler_cannot_be_cancelled(queued, mutation):
    registry, run = prepared(queued)
    path = registry.job_dir(run) / 'external_hpc.json'
    data = json.loads(path.read_text())
    if mutation in {'name', 'digest'}:
        data['name' if mutation == 'name' else 'spec_sha256'] = 'different'
        path.write_text(json.dumps(data))
    else:
        path.unlink()
        if mutation == 'symlink':
            target = path.with_name('other.json')
            target.write_text(json.dumps(data))
            path.symlink_to(target)
    with pytest.raises(ValueError):
        scope(registry, run)
    assert not (registry.job_dir(run) / 'scheduler-cancellation.json').exists()


@pytest.mark.parametrize('mutation', ['absent', 'wrong_tool', 'wrong_scope', 'terminal'])
def test_approval_and_active_state_rechecked(queued, mutation):
    registry, run = prepared(queued)
    expected = scope(registry, run)
    payload = {'tool': 'covalent_cancel_job', 'scope': expected}
    if mutation == 'wrong_tool':
        payload['tool'] = 'covalent_submit_job'
    if mutation == 'wrong_scope':
        payload['scope'] = {**expected, 'scheduler': {'name': 'another-job'}}
    if mutation == 'terminal':
        result = registry.load_result(run)
        result.state = 'COMPLETED'
        registry.write_result(result)
    token = CURRENT.set(None if mutation == 'absent' else signed(payload))
    try:
        with pytest.raises(ValueError):
            request(registry, run)
    finally:
        CURRENT.reset(token)
    assert not (registry.job_dir(run) / 'scheduler-cancellation.json').exists()


def test_tampered_intent_is_rejected(queued):
    registry, run = prepared(queued)
    token = CURRENT.set(signed({'tool': 'covalent_cancel_job', 'scope': scope(registry, run)}))
    try:
        request(registry, run)
    finally:
        CURRENT.reset(token)
    path = registry.job_dir(run) / 'scheduler-cancellation.json'
    value = json.loads(path.read_text())
    value['proof'] += 'x'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        read_intent(registry, run)


def test_native_tool_plan_and_request_bind_scheduler_without_claiming_stop(queued):
    from covscreen.approval_scope import approval_scope
    from covscreen.tool_api import cancel_job_tool
    registry, run = prepared(queued)
    expected = approval_scope(registry.project_root, 'covalent_cancel_job', {'run_id': run})
    assert expected == scope(registry, run)
    assert not (registry.job_dir(run) / 'scheduler-cancellation.json').exists()
    token = CURRENT.set(signed({'tool': 'covalent_cancel_job', 'scope': expected}))
    try:
        response = cancel_job_tool(registry.project_root, run, human_approved=True)
    finally:
        CURRENT.reset(token)
    assert response['cancellation_requested'] is True
    assert response['cancellation_confirmed'] is False
    assert response['scheduler_cancellation']['scope'] == expected
    assert response['job']['state'] == 'QUEUED'


def test_native_tool_rejects_boolean_without_signed_approval(queued):
    from covscreen.tool_api import cancel_job_tool
    registry, run = prepared(queued)
    with pytest.raises(ValueError):
        cancel_job_tool(registry.project_root, run, human_approved=True)
    assert not (registry.job_dir(run) / 'scheduler-cancellation.json').exists()


def test_scheduler_scope_change_after_approval_is_rejected(queued):
    from covscreen.approval_scope import approval_scope
    from covscreen.tool_api import cancel_job_tool
    registry, run = prepared(queued)
    expected = approval_scope(registry.project_root, 'covalent_cancel_job', {'run_id': run})
    token = CURRENT.set(signed({'tool': 'covalent_cancel_job', 'scope': expected}))
    path = registry.job_dir(run) / 'external_hpc.json'
    value = json.loads(path.read_text())
    value['name'] = 'unrelated-task'
    path.write_text(json.dumps(value))
    try:
        with pytest.raises(ValueError):
            cancel_job_tool(registry.project_root, run, human_approved=True)
    finally:
        CURRENT.reset(token)
    assert not (registry.job_dir(run) / 'scheduler-cancellation.json').exists()


@pytest.mark.parametrize('started', [False, True])
def test_scheduler_stop_only_confirms_never_started_worker(queued, started):
    from covscreen.scheduler_cancellation import confirm_unstarted
    registry, run = prepared(queued)
    expected = scope(registry, run)
    token = CURRENT.set(signed({'tool': 'covalent_cancel_job', 'scope': expected}))
    try:
        request(registry, run)
    finally:
        CURRENT.reset(token)
    if started:
        (registry.job_dir(run) / 'external_worker_claim').mkdir()
    observation = {'run_id': run, 'spec_sha256': expected['spec_sha256'],
                   'name': expected['scheduler']['name'], 'status': 'STOPPED'}
    result = confirm_unstarted(registry, run, observation)
    assert result['cancellation_confirmed'] is (not started)
    assert registry.load_result(run).state == ('QUEUED' if started else 'CANCELLED')
    if not started:
        assert confirm_unstarted(registry, run, observation) == result


def test_stopping_is_not_cancellation_confirmation(queued):
    from covscreen.scheduler_cancellation import confirm_unstarted
    registry, run = prepared(queued)
    expected = scope(registry, run)
    token = CURRENT.set(signed({'tool': 'covalent_cancel_job', 'scope': expected}))
    try:
        request(registry, run)
    finally:
        CURRENT.reset(token)
    with pytest.raises(ValueError, match='stopped scheduler'):
        confirm_unstarted(registry, run, {'run_id': run, 'spec_sha256': expected['spec_sha256'],
                         'name': expected['scheduler']['name'], 'status': 'STOPPING'})
    assert registry.load_result(run).state == 'QUEUED'


def test_get_job_exposes_intent_but_terminal_label_alone_is_not_confirmation(queued):
    from covscreen.tool_api import get_job_tool
    from covscreen.approval_scope import normalize_spec
    from covscreen.native_approval import seal_spec
    root, run = queued
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run)
    token = CURRENT.set(signed({'scope': {'kind': 'job', 'spec': normalize_spec(spec)},
                                'session': 'synthetic-session', 'message': 'synthetic-message',
                                'approval_request_id': 'per-fixture', 'tool': 'covalent_submit_job'}))
    try:
        spec.provenance['native_approval'] = seal_spec(spec)
        registry.write_spec(spec)
    finally:
        CURRENT.reset(token)
    registry, run = prepared(queued)
    expected = scope(registry, run)
    token = CURRENT.set(signed({'tool': 'covalent_cancel_job', 'scope': expected}))
    try:
        request(registry, run)
    finally:
        CURRENT.reset(token)
    result = registry.load_result(run)
    result.state = 'CANCELLED'
    registry.write_result(result)
    observed = get_job_tool(registry.project_root, run)
    assert observed['cancellation_requested']
    assert not observed['cancellation_confirmed']
    assert observed['scheduler_cancellation']['scope'] == expected
