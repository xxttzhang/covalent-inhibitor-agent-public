import hashlib
import json
import numpy as np
import pytest
from covscreen.md_platform_validation import compare, readback


def fixture(folder):
    cpu = np.array([[1., -2., 3.], [2., 3., 4.]])
    cuda = cpu + 1e-6
    files = {}
    for label, forces in [('cpu', cpu), ('cuda', cuda)]:
        path = folder / ('validation-' + label + '-forces.npy')
        np.save(path, forces)
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    value = dict(schema='openmm-platform-validation-v1', system_id='toy', parents={'input': 'sha'},
        atoms=2, integration_steps=0, platforms=['CPU', 'CUDA'], cpu_properties={'Threads': '4'},
        cuda_properties={'Precision': 'mixed'}, requested_cuda_properties={'Precision': 'mixed'},
        metrics=compare(-100., -100.00001, cpu, cuda), artifacts=files, passed=True)
    (folder/'platform-validation.json').write_text(json.dumps(value))
    return value


def check(folder):
    return readback(folder, 'toy', {'input': 'sha'}, {'Precision': 'mixed'}, 4, 2)


def test_independent_array_readback_and_tamper_rejection(tmp_path):
    fixture(tmp_path)
    assert check(tmp_path)['passed']
    np.save(tmp_path/'validation-cuda-forces.npy', np.ones((2, 3)))
    with pytest.raises(ValueError, match='forces changed'):
        check(tmp_path)


def test_recomputed_metrics_reject_false_report(tmp_path):
    value = fixture(tmp_path)
    value['metrics']['force_rms_difference_kj_mol_nm'] = 0.1
    (tmp_path/'platform-validation.json').write_text(json.dumps(value))
    with pytest.raises(ValueError, match='contradicts'):
        check(tmp_path)


@pytest.mark.parametrize('field,value', [('integration_steps', 1), ('atoms', 3), ('passed', False),
    ('parents', {'input': 'another'}), ('cuda_properties', {'Precision': 'single'})])
def test_rejects_different_state_or_platform(tmp_path, field, value):
    data = fixture(tmp_path); data[field] = value
    (tmp_path/'platform-validation.json').write_text(json.dumps(data))
    with pytest.raises(ValueError, match='frozen inputs'):
        check(tmp_path)


def test_comparison_rejects_bad_numerics():
    forces = np.ones((2, 3))
    assert not compare(-100., -101., forces, forces)['passed']
    assert not compare(-100., -100., forces, forces + 1.)['passed']
    for bad in [np.full((2, 3), np.nan), np.ones((1, 3)), np.ones((2, 2))]:
        with pytest.raises(ValueError):
            compare(-100., -100., forces, bad)
    with pytest.raises(ValueError):
        compare(float('inf'), -100., forces, forces)
