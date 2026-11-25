"""Exercise maintained route identities, data/code separation and shared contracts."""
import importlib
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from covscreen.chain_contract import ChainStatus
from covscreen.execution import _worker_environment
from covscreen.layout import config_reference, resolve_route_config, science_script
import covscreen.layout as layout

PROJECT = Path(layout.__file__).resolve().parents[2]
ROOT = PROJECT.parent


@pytest.mark.parametrize('route,module', [('ligand-first', 'ligand_chain'), ('library-first', 'library_chain')])
def test_legacy_import_is_same_maintained_module(route, module):
    legacy = importlib.import_module('covscreen.' + module)
    canonical = importlib.import_module('covscreen_' + route.replace('-', '_') + '.' + module)
    assert legacy is canonical
    assert Path(canonical.__file__).resolve().is_relative_to(ROOT / 'routes' / route / 'src')


def test_config_references_allow_only_registered_roots(tmp_path):
    from covscreen.ligand_chain import _project_input
    canonical = ROOT / 'routes/ligand-first/configs/EGFR_C797_erlotinib_v1.yaml'
    old = 'configs/ligand_first/EGFR_C797_erlotinib_v1.yaml'
    # Checkout aliases are symlinks; immutable releases materialize them.
    expected = (PROJECT / old).resolve()
    assert resolve_route_config(PROJECT, old) == expected
    assert expected.read_bytes() == canonical.read_bytes()
    assert resolve_route_config(tmp_path, canonical) == canonical
    assert resolve_route_config(PROJECT, config_reference(PROJECT, old)) == expected
    target = _project_input(PROJECT, 'configs/covindb/EGFR_C797_mvp_v1.yaml', 'AF3 target configuration')
    assert resolve_route_config(PROJECT, target) == (
        PROJECT / 'configs/covindb/EGFR_C797_mvp_v1.yaml').resolve()
    assert (PROJECT / 'configs/covindb/EGFR_C797_mvp_v1.yaml').read_bytes() == (
        ROOT / 'routes/library-first/covindb-configs/EGFR_C797_mvp_v1.yaml').read_bytes()
    # A symlink inside the registered configuration directory must not grant
    # arbitrary sibling access. Existing symlinks are resolved before checking.
    link = tmp_path / 'escape.yaml'
    link.symlink_to(ROOT / 'PLATFORM_CODE_MAP.md')
    with pytest.raises(ValueError):
        resolve_route_config(tmp_path, link)
    with pytest.raises(ValueError):
        resolve_route_config(tmp_path, ROOT / 'routes/ligand-first/README.md')


def test_data_only_project_launches_canonical_route_from_unrelated_cwd(tmp_path):
    # Poisoning the data folder must not replace the installed executable.
    bad = tmp_path / 'scripts/compile_ligand_first_chain.py'
    bad.parent.mkdir()
    bad.write_text('raise RuntimeError("wrong data-directory executable")')
    script = science_script('compile_ligand_first_chain.py')
    result = subprocess.run([sys.executable, str(script), '--help'], cwd=tmp_path,
        env=_worker_environment(tmp_path), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '--project-root' in result.stdout
    assert script.parent == ROOT / 'routes/ligand-first/scripts'


def test_shared_status_contract_keeps_route_specific_stage_validation():
    from covscreen.library_chain import LibraryFirstChainStatus
    from covscreen.ligand_chain import LigandFirstChainStatus
    common = dict(campaign_id='boundary', project_id='egfr', stages=[],
                  next_stage=None, next_parent_run_id=None, terminal_state='ready')
    library = LibraryFirstChainStatus(**common)
    ligand = LigandFirstChainStatus(**common)
    assert isinstance(library, ChainStatus) and isinstance(ligand, ChainStatus)
    assert library.model_dump() == ligand.model_dump()
    with pytest.raises(ValidationError):
        LibraryFirstChainStatus(**{**common, 'next_stage': 'covalentize'})
    assert LigandFirstChainStatus(**{**common, 'next_stage': 'covalentize'}).next_stage == 'covalentize'
