import importlib.util
import json
from pathlib import Path

import pytest

source = Path(__file__).resolve().parents[1] / "scripts/discover_ligand_first_structures.py"
spec = importlib.util.spec_from_file_location("structure_discovery", source)
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


def test_query_uses_explicit_identifiers_and_bounded_pagination():
    query = discovery.build_query("P00533", "AQ4", 20)
    assert query["request_options"]["paginate"] == {"start": 0, "rows": 20}
    assert len(query["query"]["nodes"]) == 3


@pytest.mark.parametrize("identifier", ["EGFR;echo", "P00533\n", "", "P00533`"])
def test_rejects_nonidentifier_text(identifier):
    with pytest.raises(ValueError):
        discovery.build_query(identifier, "AQ4", 20)


def test_partial_page_is_not_reported_as_complete_search():
    result = discovery.summarize({"total_count": 3,
        "result_set": [{"identifier": "1M17"}]}, {})
    assert not result["complete_search"]
    assert result["status"] == "candidates_require_structure_validation"


def test_duplicate_results_are_rejected():
    with pytest.raises(ValueError, match="inconsistency"):
        discovery.summarize({"total_count": 2,
            "result_set": [{"identifier": "1M17"}, {"identifier": "1M17"}]}, {})


@pytest.mark.parametrize("response", [{}, {"total_count": True},
    {"total_count": 1, "result_set": [{"identifier": "1M17;echo"}]},
    {"total_count": 1, "result_set": "not a list"}])
def test_malformed_responses_fail_closed(response):
    with pytest.raises(ValueError):
        discovery.summarize(response, {})


def test_tool_plan_has_no_network_or_files(tmp_path, monkeypatch):
    from covscreen.tool_api import ligand_first_discover_structures_tool
    import urllib.request
    def forbidden(*args, **kwargs):
        raise AssertionError("Plan must not send HTTP")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    result = ligand_first_discover_structures_tool(tmp_path, "P00533", "AQ4")
    assert result["network_request_sent"] is False
    assert result["campaign_registered"] is False
    assert not list(tmp_path.iterdir())


def test_tool_search_preserves_raw_provenance_without_registering(tmp_path, monkeypatch):
    from covscreen.tool_api import ligand_first_discover_structures_tool
    import urllib.request
    raw = json.dumps({"total_count": 3, "result_set": [{"identifier": "1M17"}]}).encode()
    class Reply:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return raw[:size]
    def request(req, timeout):
        assert req.full_url == discovery.ENDPOINT
        assert timeout == 30
        assert json.loads(req.data)["request_options"]["paginate"]["rows"] == 1
        return Reply()
    monkeypatch.setattr(urllib.request, "urlopen", request)
    result = ligand_first_discover_structures_tool(tmp_path, "P00533", "AQ4", limit=1, action="search")
    assert result["status"] == "PASS"
    assert result["discovery"]["complete_search"] is False
    assert result["campaign_registered"] is False
    assert result["calculation_submitted"] is False
    directory = tmp_path / result["artifact_directory"]
    assert (directory / "rcsb_response.json").read_bytes() == raw
    assert json.loads((directory / "structure_discovery.json").read_text()) == result["discovery"]
    assert not (tmp_path / "configs").exists()


@pytest.mark.parametrize("limit", [True, 1.5, 0, 101, "20"])
def test_tool_rejects_bad_limits_before_creating_artifacts(tmp_path, limit):
    from covscreen.tool_api import ligand_first_discover_structures_tool
    with pytest.raises(ValueError):
        ligand_first_discover_structures_tool(tmp_path, "P00533", "AQ4", limit=limit, action="search")
    assert not list(tmp_path.iterdir())
