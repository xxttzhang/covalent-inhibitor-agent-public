"""Later batch actions consume exact frozen stages without preparing them again."""
import pytest
from covscreen.layout import enable_de_novo
from covscreen.mcp.de_novo import batch_review

enable_de_novo()
from covscreen_de_novo import batch_lo, batch_glide


@pytest.mark.parametrize('action', ['plan_lo', 'status_lo', 'evaluate_lo'])
def test_lo_actions_use_bound_existing_input(monkeypatch, tmp_path, action):
    calls = []
    def no_freeze(*args):
        pytest.fail('Observing/planning must not repeat prepare_lo')
    monkeypatch.setattr('covscreen_de_novo.batch_lo_input.freeze', no_freeze)
    def load(root, digest):
        assert digest == 'b' * 64
        return {'batch_manifest_sha256': 'a' * 64}, tmp_path
    monkeypatch.setattr('covscreen_de_novo.batch_lo_input.load', load)
    monkeypatch.setattr('covscreen_de_novo.batch_lo.plan', lambda root, digest: calls.append(('plan', digest)))
    monkeypatch.setattr('covscreen_de_novo.batch_lo.observe',
        lambda root, digest, evaluate=False: calls.append(('observe', digest, evaluate)))
    batch_review(tmp_path, {'action': action, 'manifest_sha256': 'a' * 64, 'lo_manifest_sha256': 'b' * 64})
    assert calls == ([('plan', 'b' * 64)] if action == 'plan_lo'
        else [('observe', 'b' * 64, action == 'evaluate_lo')])
    with pytest.raises(ValueError, match='different VS batch'):
        batch_review(tmp_path, {'action': action, 'manifest_sha256': 'c' * 64, 'lo_manifest_sha256': 'b' * 64})
    with pytest.raises(ValueError, match='frozen LO input manifest'):
        batch_review(tmp_path, {'action': action, 'manifest_sha256': 'a' * 64})


def test_glide_status_does_not_prepare_predecessors(monkeypatch, tmp_path):
    def no_freeze(*args):
        pytest.fail('Status must not reconstruct prior stages')
    monkeypatch.setattr('covscreen_de_novo.batch_lo_input.freeze', no_freeze)
    monkeypatch.setattr('covscreen_de_novo.batch_prereaction_input.freeze', no_freeze)
    monkeypatch.setattr('covscreen_de_novo.batch_lo_input.load',
        lambda root, digest: ({'batch_manifest_sha256': 'a' * 64}, tmp_path))
    monkeypatch.setattr('covscreen_de_novo.batch_prereaction_input.load',
        lambda root, source, digest: ({'source': {'lo_manifest_sha256': 'b' * 64},
            'source_sha256': source, 'manifest_sha256': digest}, tmp_path))
    monkeypatch.setattr('covscreen_de_novo.batch_glide.observe',
        lambda root, source, digest, evaluate: {'source': source, 'manifest': digest, 'evaluate': evaluate})
    args = {'action': 'status_prereaction_batch', 'manifest_sha256': 'a' * 64,
        'lo_manifest_sha256': 'b' * 64, 'prereaction_source_sha256': 'c' * 64,
        'prereaction_manifest_sha256': 'd' * 64}
    assert batch_review(tmp_path, args) == {'source': 'c' * 64, 'manifest': 'd' * 64, 'evaluate': False}
    with pytest.raises(ValueError, match='different LO batch'):
        batch_review(tmp_path, {**args, 'lo_manifest_sha256': 'e' * 64})
