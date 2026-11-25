from pathlib import Path

import pytest

from covscreen.layout import library_script, resolve_library_config


def test_data_only_campaign_uses_installed_route_command(tmp_path):
    entry = library_script(tmp_path, 'run_covindb_prescreen.py')
    assert entry == Path(__file__).resolve().parents[1] / 'scripts/run_covindb_prescreen.py'
    assert entry.is_file() and not entry.is_symlink()


def test_registered_config_crosses_only_declared_route_boundary(tmp_path):
    route = Path(__file__).resolve().parents[1]
    source = route / 'covindb-configs/EGFR_C797_mvp_v1.yaml'
    legacy = tmp_path / 'target.yaml'
    legacy.symlink_to(source)
    assert resolve_library_config(tmp_path, legacy) == source
    with pytest.raises(ValueError, match='outside'):
        resolve_library_config(tmp_path, route / 'README.md')
    with pytest.raises(ValueError, match='basename'):
        library_script(tmp_path, '../outside.py')
