"""Postprocessing recovery must never admit a failed vendor computation."""
from types import SimpleNamespace

import pytest

from covscreen.covdock_vs_evidence import products


@pytest.mark.parametrize('reason,code', [
    ('RuntimeError: vendor failed', None),
    ('ValueError: VS target SG is not attached to the declared beta carbon', 1),
    ('ValueError: VS output candidate index differs from approved input order', None),
])
def test_other_failed_jobs_cannot_be_recovered(monkeypatch, tmp_path, reason, code):
    monkeypatch.setattr('covscreen.native_approval.verify_spec', lambda spec: None)
    monkeypatch.setattr('covscreen.source_release.verify_source', lambda spec: None)
    spec = SimpleNamespace(engine='schrodinger-covdock-vs', route='de-novo',
        project_id='flow', run_id='run')
    result = SimpleNamespace(state='FAILED', return_code=code, failure_reason=reason)
    with pytest.raises(ValueError, match='completed registered'):
        products(tmp_path, {'workflow_id': 'flow', 'vs_execution': {'run_id': 'run'}},
            None, spec, result, reaction_site_rejections=[])


def test_other_routes_keep_strict_readback(monkeypatch, tmp_path):
    monkeypatch.setattr('covscreen.native_approval.verify_spec', lambda spec: None)
    spec = SimpleNamespace(route='library-first')
    result = SimpleNamespace(state='FAILED', return_code=None,
        failure_reason='ValueError: VS target SG is not attached to the declared beta carbon')
    with pytest.raises(ValueError, match='only for De novo'):
        products(tmp_path, {}, None, spec, result, reaction_site_rejections=[])
