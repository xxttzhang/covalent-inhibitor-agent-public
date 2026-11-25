"""Bounded dependency probe; timeout means unconfirmed, not absent hardware."""
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

DEFAULT_TIMEOUT_SECONDS = 120
MARKER = 'COVSCREEN_PROBE '
PROGRAM = r'''
import importlib, json, sys, time
versions, timings = {}, {}
def event(**values):
    print('COVSCREEN_PROBE ' + json.dumps(values), file=sys.stderr, flush=True)
for name in ('torch', 'rdkit', 'dgl'):
    event(stage=name, state='running', timings_seconds=timings)
    started = time.monotonic()
    module = importlib.import_module(name)
    timings[name] = round(time.monotonic() - started, 3)
    versions[name] = module.__version__
event(stage='cuda_inventory', state='running', timings_seconds=timings)
import torch
cuda = {'available': torch.cuda.is_available(), 'device_count': torch.cuda.device_count(),
        'devices': [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]}
event(stage='complete', state='complete', versions=versions, cuda=cuda, timings_seconds=timings)
'''


def timeout_seconds(config: Mapping[str, Any]) -> int:
    section = config.get('runtime_preflight', {})
    if not isinstance(section, dict):
        raise ValueError('runtime_preflight must be an object')
    value = section.get('timeout_seconds', DEFAULT_TIMEOUT_SECONDS)
    if type(value) is not int or not 1 <= value <= 600:
        raise ValueError('runtime_preflight.timeout_seconds must be an integer in [1, 600]')
    return value


def _progress(stderr):
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors='replace')
    events = []
    for line in (stderr or '').splitlines():
        if line.startswith(MARKER):
            try:
                events.append(json.loads(line[len(MARKER):]))
            except json.JSONDecodeError:
                pass
    return events[-1] if events else {}


def probe_python_runtime(executable: Path, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    timeout_seconds({'runtime_preflight': {'timeout_seconds': timeout}})
    started = time.monotonic()
    def result(status, progress=None, **values):
        progress = progress or {}
        return {'status': status, 'timeout_seconds': timeout,
                'elapsed_seconds': round(time.monotonic() - started, 3),
                'stage': progress.get('stage', 'process_start'),
                'timings_seconds': progress.get('timings_seconds', {}), **values}
    try:
        completed = subprocess.run([str(executable), '-u', '-c', PROGRAM],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        return result('FAIL', _progress(error.stderr), error_kind='TIMEOUT',
            error=f'Runtime probe exceeded {timeout} seconds; environment availability is unconfirmed. No automatic retry.')
    except OSError as error:
        return result('FAIL', error_kind='START_ERROR', error=f'Runtime probe could not start: {error}')
    progress = _progress(completed.stderr)
    if completed.returncode != 0:
        lines = completed.stderr.strip().splitlines()
        return result('FAIL', progress, error_kind='DEPENDENCY_ERROR',
            error=lines[-1] if lines else f'Runtime probe exited {completed.returncode}')
    if progress.get('state') != 'complete' or not isinstance(progress.get('versions'), dict) or not isinstance(progress.get('cuda'), dict):
        return result('FAIL', progress, error_kind='INVALID_OUTPUT', error='Runtime probe returned no complete result')
    return result('PASS', progress, versions=progress['versions'], cuda=progress['cuda'])
