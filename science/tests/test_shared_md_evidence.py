"""Shared verifiers load without any discovery-route package; legacy imports retain identity."""
import importlib
import os
from pathlib import Path
import subprocess
import sys


def test_shared_verifiers_do_not_load_discovery_routes():
    source = Path(__file__).resolve().parents[1] / 'src'
    script = '''
import sys
from covscreen.md_adduct_input import admit_covalent_adduct_input
from covscreen.md_candidate_pose import load_candidate_pose_input
from covscreen.md_parameter_contract import parameter_contract
from covscreen.md_parameterization_evidence import parameterization_receipt
from covscreen.md_preparation_evidence import preparation_receipt
from covscreen.md_short_qc_evidence import short_qc_receipt
from covscreen.md_equilibration_evidence import equilibration_receipt
assert not any(name.startswith(('covscreen_library_first', 'covscreen_ligand_first', 'covscreen_de_novo')) for name in sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', script], env={**os.environ, 'PYTHONPATH': str(source)},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_legacy_imports_share_one_implementation():
    import covscreen.library_workflow  # Registers the existing route package path.
    for module, names in {
        'md_candidate_pose': ('freeze_candidate_pose', 'freeze_delivered_candidate_pose', 'load_candidate_pose_input'),
        'md_adduct_input': ('SUPPORTED_REACTIONS', 'admit_covalent_adduct_input', 'load_covalent_adduct_directory', 'load_covalent_adduct_input'),
        'md_preparation_evidence': ('registered_output', 'preparation_receipt'),
        'md_short_qc_evidence': ('short_qc_receipt',),
        'md_equilibration_evidence': ('equilibration_receipt',),
    }.items():
        shared = importlib.import_module('covscreen.' + module)
        legacy = importlib.import_module('covscreen_library_first.' + module)
        for name in names:
            assert getattr(shared, name) is getattr(legacy, name)


def test_conversion_retains_shared_parameter_contract():
    import covscreen.library_workflow
    from covscreen.md_parameter_contract import parameter_contract
    from covscreen_library_first.md_system_conversion import _parameter_contract
    assert _parameter_contract is parameter_contract


def test_native_pose_reader_import_does_not_require_tabular_runtime():
    source = Path(__file__).resolve().parents[1] / 'src'
    script = '''
import sys
from covscreen.md_candidate_pose import load_candidate_pose_input
from covscreen.pose_identity import exact_pose_index
assert exact_pose_index('0.0') == 0
assert not any(name.startswith(('pandas', 'numpy', 'pyarrow')) for name in sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', script],
        env={**os.environ, 'PYTHONPATH': str(source)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
