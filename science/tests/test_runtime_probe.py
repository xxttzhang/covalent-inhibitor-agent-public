import subprocess
import sys
from pathlib import Path

import pytest

from covscreen import runtime_probe as probe
from covscreen.runtime_health import summarize_runtime_preflight


@pytest.mark.parametrize('value', [0, 601, True, '120', 1.5, None])
def test_invalid_deadline_is_rejected(value):
    with pytest.raises(ValueError):
        probe.timeout_seconds({'runtime_preflight': {'timeout_seconds': value}})


def test_deadline_defaults_and_override():
    assert probe.timeout_seconds({}) == 120
    assert probe.timeout_seconds({'runtime_preflight': {'timeout_seconds': 180}}) == 180


def test_real_child_timeout_retains_stage_and_stops(tmp_path, monkeypatch):
    marker = tmp_path / 'finished'
    monkeypatch.setattr(probe, 'PROGRAM',
        "import sys,time;print('COVSCREEN_PROBE {\"stage\":\"torch\",\"state\":\"running\"}',file=sys.stderr,flush=True);time.sleep(10);open(" + repr(str(marker)) + ", 'w').write('bad')")
    result = probe.probe_python_runtime(Path(sys.executable), timeout=1)
    assert result['status'] == 'FAIL'
    assert result['error_kind'] == 'TIMEOUT'
    assert result['stage'] == 'torch'
    assert not marker.exists()
    summary = summarize_runtime_preflight(probe=result, engines={}, executable=sys.executable)
    assert summary['resource_classes']['cpu'] == {'status': 'UNKNOWN', 'available': None}
    assert summary['resource_classes']['cuda']['status'] == 'UNKNOWN'
    assert summary['cuda']['available'] is None
    assert summary['status'] == 'FAIL'


def test_success_ignores_library_stdout_noise(monkeypatch):
    monkeypatch.setattr(probe, 'PROGRAM', '''import sys
print('library startup message')
print('COVSCREEN_PROBE {"stage":"complete","state":"complete","versions":{"torch":"test"},"cuda":{"available":false,"device_count":0,"devices":[]},"timings_seconds":{"torch":0.1}}',file=sys.stderr)
''')
    result = probe.probe_python_runtime(Path(sys.executable), timeout=5)
    assert result['status'] == 'PASS'
    assert result['timings_seconds']['torch'] == 0.1


@pytest.mark.parametrize('program,kind', [("raise ImportError('broken dependency')", 'DEPENDENCY_ERROR'), ('print("bad")','INVALID_OUTPUT')])
def test_errors_are_distinguished(monkeypatch, program, kind):
    monkeypatch.setattr(probe, 'PROGRAM', program)
    result = probe.probe_python_runtime(Path(sys.executable), timeout=5)
    assert result['status'] == 'FAIL'
    assert result['error_kind'] == kind


def test_missing_executable():
    assert probe.probe_python_runtime(Path('/nonexistent/probe-python'))['error_kind'] == 'START_ERROR'
