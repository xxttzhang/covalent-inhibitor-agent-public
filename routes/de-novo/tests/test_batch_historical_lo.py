"""Software-only fixture: real isolated replay and immutable release checks."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from covscreen import native_approval, source_release
from covscreen_de_novo.batch_historical_lo import evaluate


@pytest.fixture
def historical(tmp_path, monkeypatch):
    files = {
        'science/src/covscreen/__init__.py': b'',
        'science/src/covscreen/source_release.py': Path(source_release.__file__).read_bytes(),
        'science/src/covscreen/native_approval.py': b"def verify_spec(spec):\n    assert spec.signature == 'fixture-signed'\n",
        'science/src/covscreen/execution.py': b'''
import json
from pathlib import Path
from types import SimpleNamespace
class JobManager:
    def __init__(self, root):
        self.root, self.registry = Path(root), self
    def load_spec(self, run):
        spec = SimpleNamespace(**json.loads((self.root / 'spec.json').read_text()))
        assert spec.run_id == run
        return spec
    def load_result(self, run):
        return None
''',
        'science/src/covscreen/ligand_covdock_evidence.py': b'''
def evaluate_outputs(root, state, manager, spec, result, *, route):
    assert route == 'de-novo'
    assert state['refinement_input']['artifact_id'] == 'denovo-batch-lo-' + 'a'*64
    return {'schema': 'denovo-covdock-measurement-review-v1', 'run_id': spec.run_id,
            'reader': __file__, 'scientific_fixture': True}
'''}
    manifest = {'version': 'screening-code-release-v1',
                'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    release = tmp_path / hashlib.sha256(raw).hexdigest()
    for name, data in files.items():
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (release / 'manifest.json').write_bytes(raw + b'\n')
    reference = {key: value for key, value in source_release._read_release(release).items() if key != 'files'}
    spec = SimpleNamespace(run_id='fixture-lo', route='de-novo', engine='schrodinger-covdock-lo',
        signature='fixture-signed', provenance={'source_release': reference},
        parameters={'denovo_batch_lo_sha256': 'a'*64})
    root = tmp_path / 'data'
    root.mkdir()
    (root / 'spec.json').write_text(json.dumps(vars(spec)))
    monkeypatch.setattr(native_approval, 'verify_spec', lambda value: None)
    return root, spec, release


def test_replay_uses_signed_original_reader_and_worker_stays_strict(historical, monkeypatch):
    root, spec, release = historical
    monkeypatch.setattr(source_release, 'frozen_release', lambda: {'root': 'different-current-release'})
    with pytest.raises(ValueError, match='Worker code differs'):
        source_release.verify_source(spec)
    result = evaluate(root, spec)
    assert result['reader'] == str(release / 'science/src/covscreen/ligand_covdock_evidence.py')
    assert result['scientific_fixture']
    assert not list(release.rglob('__pycache__'))


def test_replay_rejects_tampered_original_reader(historical):
    root, spec, release = historical
    (release / 'science/src/covscreen/ligand_covdock_evidence.py').write_text('raise RuntimeError()')
    with pytest.raises(ValueError, match='file changed'):
        evaluate(root, spec)


def test_child_rechecks_registered_signature(historical):
    root, spec, _ = historical
    changed = {**vars(spec), 'signature': 'tampered'}
    (root / 'spec.json').write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='Historical LO readback failed'):
        evaluate(root, spec)
