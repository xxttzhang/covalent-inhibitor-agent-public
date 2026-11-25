from pathlib import Path
import sys

import pytest

OPS = (Path(__file__).resolve().parents[3] / "science") / 'ops'
sys.path.insert(0, str(OPS))
import dispatch_ligand_smoke as dispatch


def record():
    return {'run_id': 'test', 'spec_sha256': 'a' * 64, 'name': 'ligand-smoke-test',
            'workload': 'job', 'minutes': 30, 'engine': 'alphafold3-mpae',
            'budget': {'gpu_count': 1, 'cpu_count': 20, 'memory_gb': 200}}


def test_exclusion_preserves_budget_and_worker():
    baseline = dispatch.create_args(record())
    changed = dispatch.create_args(record(), ['qb-prod-gpu997', 'qb-prod-gpu997'])
    assert changed == baseline + ['--exclude-node', 'qb-prod-gpu997']
    assert changed[changed.index('--max-time') + 1] == '0.5'
    assert '--no-auto-fault-tolerance' in changed


def test_invalid_node_rejected():
    with pytest.raises(ValueError):
        dispatch.create_args(record(), ['node;touch /tmp/x'])


def test_cpu_exclusion_rejected():
    value = record()
    value.update(workload='hpc', budget={'gpu_count': 0, 'cpu_count': 4, 'memory_gb': 16})
    with pytest.raises(ValueError):
        dispatch.create_args(value, ['qb-prod-gpu997'])


def test_suspended_engine_cannot_create_or_replace_scheduler():
    value = record()
    value['engine'] = 'carsidock-cov'
    with pytest.raises(ValueError, match='docking_engine_disabled'):
        dispatch.create_args(value)
    with pytest.raises(ValueError, match='docking_engine_disabled'):
        dispatch.create_args(value, ['qb-prod-gpu997'])
