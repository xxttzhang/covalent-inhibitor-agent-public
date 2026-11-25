"""Durable observed workflow snapshots, never an execution authorization."""
import fcntl
import hashlib
import json
from pathlib import Path

from .jobs import ArtifactRegistry, sha256_file, utc_now
from .native_approval import approval_observation


def persist_chain_observation(root: Path, config_path: Path, response: dict) -> dict:
    root = Path(root).resolve()
    config_path = Path(config_path).resolve()
    identity = hashlib.sha256(str(config_path).encode()).hexdigest()
    folder = root / 'state' / 'workflows' / identity
    folder.mkdir(parents=True, exist_ok=True)
    references = []
    registry = ArtifactRegistry(root)
    for stage in response['chain']['stages']:
        run_id = stage.get('run_id')
        if not run_id:
            continue
        spec = registry.load_spec(run_id)
        references.append({'stage': stage['stage'], 'run_id': run_id,
                           'spec_sha256': sha256_file(registry.job_dir(run_id) / 'spec.json'),
                           'result_sha256': sha256_file(registry.job_dir(run_id) / 'result.json'),
                           'input_sha256': spec.input_sha256,
                           'approval_observation': approval_observation(spec),
                           'resource_budget': spec.resource_budget.model_dump(mode='json')})
    request = response.get('next_request')
    # Only persist plan content; a model's approval flag/note is not authorization evidence.
    plan = ({k: v for k, v in request.items() if k not in {'human_approved', 'approval_note'}} if request else None)
    payload = {'schema': 'workflow-observation-v1', 'observed_at': utc_now(),
               'config_path': str(config_path), 'config_sha256': sha256_file(config_path),
               'chain': response['chain'], 'route_plan': response.get('route_plan'), 'inputs': references, 'next_request': plan,
               'execution_readiness_observation': response.get('execution_readiness'),
               'submitted_run_id': response.get('job', {}).get('run_id'),
               'approval_required_for_execution': True,
               'resume': {'tool': 'covalent_library_first_chain',
                          'arguments': {'config_path': str(config_path), 'execution_mode': 'plan', 'submit': False}},
               'resume_policy': 'Recompute from live registry and reverify inputs. This snapshot is not authority to advance or execute.'}
    with (folder / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = folder / 'latest.json'
        revision = json.loads(previous.read_text()).get('revision', 0) + 1 if previous.exists() else 1
        payload['revision'] = revision
        ArtifactRegistry._write_json(folder / f'{revision:08d}.json', payload)
        ArtifactRegistry._write_json(previous, payload)
    return {'workflow_id': identity, 'revision': revision, 'path': str(previous.relative_to(root)),
            'sha256': sha256_file(previous), 'resume': payload['resume'],
            'approval_observations': [
                {'stage': item['stage'], 'run_id': item['run_id'], **item['approval_observation']}
                for item in references]}


def list_workflows(root: Path, limit: int = 50) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be an integer between 1 and 100')
    folder = Path(root).resolve() / 'state' / 'workflows'
    entries = []
    for path in sorted(folder.glob('*/latest.json')):
        if path.is_symlink() or not path.resolve().is_relative_to(folder):
            raise ValueError('Workflow snapshot escaped registry')
        payload = json.loads(path.read_text())
        entries.append({'workflow_id': path.parent.name, 'revision': payload['revision'],
                        'observed_at': payload['observed_at'],
                        'campaign_id': payload['chain']['campaign_id'],
                        'last_observed_state': payload['chain']['terminal_state'],
                        'requires_live_revalidation': True})
    entries.sort(key=lambda item: item['observed_at'], reverse=True)
    return {'status': 'PASS', 'workflows': entries[:limit], 'count': len(entries),
            'calculation_submitted': False}


def resume_workflow(root: Path, workflow_id: str) -> dict:
    import re
    if not re.fullmatch(r'[a-f0-9]{64}', workflow_id):
        raise ValueError('Invalid workflow_id')
    root = Path(root).resolve()
    folder = root / 'state' / 'workflows'
    path = folder / workflow_id / 'latest.json'
    if path.is_symlink() or not path.resolve().is_relative_to(folder):
        raise ValueError('Workflow snapshot escaped registry')
    payload = json.loads(path.read_text())
    config = Path(payload['config_path']).resolve()
    if hashlib.sha256(str(config).encode()).hexdigest() != workflow_id:
        raise ValueError('Workflow configuration identity mismatch')
    if sha256_file(config) != payload['config_sha256']:
        raise ValueError('Workflow configuration changed; create a reviewed new plan')
    from .tool_api import library_first_chain_tool
    response = library_first_chain_tool(root, str(config), execution_mode='plan', submit=False)
    response['resumed_from_revision'] = payload['revision']
    response['calculation_submitted'] = False
    return response
