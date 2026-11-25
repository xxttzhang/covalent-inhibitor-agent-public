import json
import pytest

from covscreen.runtime_health import (
    resolve_openscience_config,
    resolve_openscience_port,
    summarize_runtime_preflight,
)


def test_health_port_matches_deployed_host_and_keeps_local_default():
    assert resolve_openscience_port({"OPENSCIENCE_PORT": "4098"}) == 4098
    assert resolve_openscience_port({}) == 4096


@pytest.mark.parametrize("value", ["", "0", "65536", "http://localhost", "4098.0", "-1"])
def test_invalid_explicit_port_is_not_silently_replaced(value):
    with pytest.raises(ValueError):
        resolve_openscience_port({"OPENSCIENCE_PORT": value})


def test_runtime_preflight_keeps_cpu_tools_available_without_cuda():
    result = summarize_runtime_preflight(
        probe={
            "status": "PASS",
            "versions": {"torch": "test", "dgl": "test", "rdkit": "test"},
            "cuda": {"available": False, "device_count": 0, "devices": []},
        },
        engines={"carsidock_cov": True, "rtmscore": True},
        executable="/registered/python",
        environment={},
    )

    assert result["status"] == "PASS"
    assert result["resource_classes"]["cpu"] == {
        "status": "AVAILABLE", "available": True,
    }
    assert result["resource_classes"]["cuda"]["status"] == "UNAVAILABLE"
    assert result["limitations"] == [
        "cuda_unavailable_gpu_tools_blocked_cpu_tools_may_run"
    ]
    assert "gpu_count > 0" in result["decision_rule"]
    assert result['resource_budget_fit'] == 'NOT_VERIFIED'
    assert result['resource_reservation'] == 'NONE'
    from datetime import datetime
    assert datetime.fromisoformat(result['observed_at']).utcoffset() is not None


def test_failed_probe_leaves_resources_unknown_and_still_fails():
    result = summarize_runtime_preflight(
        probe={"status": "FAIL", "error": "runtime missing"},
        engines={},
        executable="/missing/python",
        environment={},
    )

    assert result["status"] == "FAIL"
    assert result["resource_classes"]["cpu"]["available"] is None
    assert result["resource_classes"]["cuda"]["available"] is None
    assert result["probe_error"] == "runtime missing"


@pytest.mark.parametrize('cuda', [None, {}, [], {'available': 'false', 'device_count': 1},
    {'available': True, 'device_count': True}, {'available': True, 'device_count': '1'},
    {'available': True, 'device_count': 0}, {'available': False, 'device_count': 1},
    {'available': False, 'device_count': -1}])
def test_missing_or_inconsistent_cuda_observation_stays_unknown(cuda):
    result = summarize_runtime_preflight(probe={'status': 'PASS', 'cuda': cuda},
        engines={'carsidock_cov': True}, executable='/python', environment={})
    assert result['resource_classes']['cuda'] == {
        'status': 'UNKNOWN', 'available': None, 'device_count': None}
    assert result['resource_classes']['cpu']['available'] is True
    assert result['resource_budget_fit'] == 'NOT_VERIFIED'
    assert 'cuda_unconfirmed_missing_or_inconsistent_probe_evidence' in result['limitations']


def test_positive_gpu_observation_does_not_prove_whole_budget_or_reservation():
    result = summarize_runtime_preflight(probe={'status': 'PASS',
        'cuda': {'available': True, 'device_count': 1}}, engines={}, executable='/python', environment={})
    assert result['resource_classes']['cuda']['status'] == 'AVAILABLE'
    assert result['resource_budget_fit'] == 'NOT_VERIFIED'
    assert result['resource_reservation'] == 'NONE'


def test_health_config_resolution_accepts_active_project_outside_data_root(tmp_path):
    data_root = tmp_path / "study-data"
    runtime_project = tmp_path / "runtime-project"
    data_root.mkdir()
    runtime_project.mkdir()
    active = runtime_project / "openscience.json"
    active.write_text(json.dumps({"mcp": {"covalent-discovery": {}}}) + "\n")

    found, checked = resolve_openscience_config(
        data_root,
        cwd=runtime_project,
        environment={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
        home=tmp_path / "home",
    )

    assert found == active
    assert str(data_root / "openscience.json") in checked


def test_invalid_explicit_config_falls_through_to_active_project(tmp_path):
    data_root = tmp_path / "data"
    runtime_project = tmp_path / "runtime"
    data_root.mkdir()
    runtime_project.mkdir()
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json\n")
    active = runtime_project / "openscience.json"
    active.write_text("{}\n")

    found, checked = resolve_openscience_config(
        data_root,
        cwd=runtime_project,
        environment={"OPENSCIENCE_CONFIG": str(invalid)},
        home=tmp_path / "home",
    )

    assert checked[0] == str(invalid)
    assert found == active
