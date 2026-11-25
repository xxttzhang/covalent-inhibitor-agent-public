"""Progress separation must not change scientific arguments or other engines."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

PATH = Path(__file__).resolve().parents[1] / 'scripts/covdock_isolated_progress.py'
SPEC = importlib.util.spec_from_file_location('covdock_progress_adapter', PATH)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


def test_nested_pose_progress_cannot_index_outer_candidate_array():
    class ExecuteFunction:
        JOBCONTROL_FLAG = 'EXECUTE_FUNCTION_PROGRESS'
    class CovDock(ExecuteFunction):
        pass
    class LigandStrain(ExecuteFunction):
        pass
    adapter.configure_progress(CovDock, ExecuteFunction)
    states = [0, 0]
    # Mirrors the vendor message-name filter before its task_state[index].
    for channel, index in [(LigandStrain.JOBCONTROL_FLAG, 2),
                           (CovDock.JOBCONTROL_FLAG, 0),
                           (CovDock.JOBCONTROL_FLAG, 1)]:
        if channel == CovDock.JOBCONTROL_FLAG:
            states[index] = 1
    assert states == [1, 1]
    assert ExecuteFunction.JOBCONTROL_FLAG == LigandStrain.JOBCONTROL_FLAG
    assert LigandStrain.JOBCONTROL_FLAG == 'EXECUTE_FUNCTION_PROGRESS'


def test_launcher_reenters_itself_with_unchanged_scientific_options(monkeypatch):
    calls = []
    class ExecuteFunction:
        JOBCONTROL_FLAG = 'EXECUTE_FUNCTION_PROGRESS'
    class CovDock(ExecuteFunction):
        def __init__(self, script):
            calls.append(script)
        def launch(self, arguments):
            calls.append((self.JOBCONTROL_FLAG, arguments))
    packages = SimpleNamespace(covalent_docking=SimpleNamespace(CovalentDocking=CovDock),
        executeparallel=SimpleNamespace(ExecuteFunction=ExecuteFunction))
    monkeypatch.setitem(sys.modules, 'schrodinger.application.prime.packages', packages)
    arguments = ['receptor.mae', 'ligands.mae', 'A:773', '-mode', 'leadopt',
                 '-NJOBS', '2', '-affinity', '-strain', '-HOST', 'localhost:2', '-WAIT']
    monkeypatch.setattr(sys, 'argv', [str(PATH), *arguments])
    adapter.main()
    assert calls == [str(PATH), (adapter.PROGRESS_CHANNEL, arguments)]


def test_changed_vendor_contract_requires_revalidation():
    class ExecuteFunction:
        JOBCONTROL_FLAG = 'NEW_VENDOR_CHANNEL'
    class CovDock(ExecuteFunction):
        pass
    with pytest.raises(RuntimeError, match='revalidate'):
        adapter.configure_progress(CovDock, ExecuteFunction)
