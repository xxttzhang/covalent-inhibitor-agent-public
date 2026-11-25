"""Input binding and packaging tests; these do not claim native force-field validation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from covscreen_library_first.md_parameterization import parameterize, read_input


def inputs(tmp_path, **updates):
    # Opaque bytes exercise receipt binding only. No test treats them as a
    # structure or fabricates a successful vendor calculation.
    source = tmp_path / 'input.mae'
    source.write_bytes(b'input-binding-test\n')
    value = dict(system_id='new_candidate', status='LOOP_GRAPH_QC_PASSED_PENDING_RELAXATION',
                 simulation_state='covalent_adduct', heavy_graph_preserved=True, warnings=[],
                 loop_qc_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                 covalent_atom_indices_1based=[1, 10],
                 ligand_heavy_atom_indices_1based=[1, 2, 3, 4, 5])
    value.update(updates)
    receipt = tmp_path / 'qc.json'
    receipt.write_text(json.dumps(value))
    return source, receipt, hashlib.sha256(receipt.read_bytes()).hexdigest()


def test_receipt_is_bound_to_both_files_before_native_loading(tmp_path):
    args = inputs(tmp_path)
    assert read_input(*args)['system_id'] == 'new_candidate'
    with pytest.raises(ValueError, match='QC checksum'):
        read_input(args[0], args[1], '0' * 64)
    args[0].write_bytes(b'changed')
    with pytest.raises(ValueError, match='Adduct checksum'):
        parameterize(*args, tmp_path / 'output', tmp_path / 'vendor')
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize('update', [
    {'system_id': '../escape'}, {'warnings': ['unresolved clash']},
    {'heavy_graph_preserved': False}, {'status': 'WARNING'},
    {'simulation_state': 'noncovalent_complex'},
    {'covalent_atom_indices_1based': [True, 10]},
    {'covalent_atom_indices_1based': [1, 1]},
    {'covalent_atom_indices_1based': [10, 1]},
    {'ligand_heavy_atom_indices_1based': [1, 2, 3, 4, 10]},
    {'ligand_heavy_atom_indices_1based': [1, 2, 3, 4, 4]},
])
def test_invalid_adduct_receipts_stop_before_vendor_or_outputs(tmp_path, update):
    args = inputs(tmp_path, **update)
    with pytest.raises(ValueError):
        parameterize(*args, tmp_path / 'output', tmp_path / 'vendor')
    assert not (tmp_path / 'output').exists()


def test_existing_output_is_never_overwritten(tmp_path):
    args = inputs(tmp_path)
    output = tmp_path / 'output'
    output.mkdir()
    marker = output / 'evidence'
    marker.write_text('preserve')
    with pytest.raises(ValueError, match='new directory'):
        parameterize(*args, output, tmp_path / 'vendor')
    assert marker.read_text() == 'preserve'


def test_wrong_system_identity_and_dangling_output_link_are_rejected(tmp_path):
    args = inputs(tmp_path)
    with pytest.raises(ValueError, match='system identity'):
        parameterize(*args, tmp_path / 'output', tmp_path / 'vendor', expected_system_id='other')
    output = tmp_path / 'link'
    output.symlink_to(tmp_path / 'missing-output')
    with pytest.raises(ValueError, match='new directory'):
        parameterize(*args, output, tmp_path / 'vendor')
    assert not (tmp_path / 'missing-output').exists()


def test_control_package_launcher_imports_canonical_module_and_rejects_bad_qc_under_optimization(tmp_path):
    route = Path(__file__).resolve().parents[1]
    package = tmp_path / 'control'
    package.mkdir()
    launcher = 'parameterize_library_single_adduct_20261009.py'
    shutil.copyfile(route / 'historical-ops' / launcher, package / launcher)
    shutil.copyfile(route / 'src/covscreen_library_first/md_parameterization.py',
                    package / 'md_parameterization.py')
    source = tmp_path / 'data/loop-repair-v2/new_candidate'
    source.mkdir(parents=True)
    a, b, digest = inputs(source, warnings=['unresolved clash'])
    a.rename(source / 'loop-qc-input.mae')
    b.rename(source / 'loop-qc.json')
    result = subprocess.run([sys.executable, '-B', '-O', str(package / launcher),
                             str(tmp_path / 'data'), 'new_candidate', digest],
                            cwd=tmp_path, env={**os.environ, 'PYTHONPATH': ''},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Prepared-adduct graph QC has not passed' in result.stderr
    assert 'ModuleNotFoundError' not in result.stderr
    assert not (tmp_path / 'data/parameters-v2').exists()
