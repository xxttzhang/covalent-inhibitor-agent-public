import json
from pathlib import Path

import pytest
from covscreen.target_hypothesis import finalize, read, folder, digest, save, run_collection


@pytest.mark.parametrize("target", ["EGFR", "P00533"])
def test_collector_resolves_exact_gene_or_accession(target):
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("collector", Path(__file__).resolve().parents[1] / "scripts/collect_hypothesis_evidence.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    entry = {"primaryAccession": "P00533", "genes": [{"geneName": {"value": "EGFR"}}],
        "organism": {"taxonId": 9606}, "entryType": "UniProtKB reviewed (Swiss-Prot)",
        "sequence": {"value": "AAC"}}
    queries = []
    def request_get(url, **kwargs):
        queries.append(kwargs["params"]["query"])
        return SimpleNamespace(headers={"x-total-results": "1"}, raise_for_status=lambda: None,
            json=lambda: {"results": [entry]})
    identity = adapter.resolve_identity(request_get, target, "C3", 9606)
    assert identity["gene"] == "EGFR"
    assert identity["uniprot_id"] == "P00533"
    assert identity["requested_target"] == target
    field = "accession" if target == "P00533" else "gene_exact"
    assert queries[0] == f"({field}:{target}) AND organism_id:9606 AND reviewed:true"
    assert " OR " not in queries[0]
    for field, value in [("organism", {"taxonId": 10090}),
                         ("entryType", "UniProtKB unreviewed (TrEMBL)"),
                         ("sequence", {"value": "AAA"})]:
        original = entry[field]
        entry[field] = value
        with pytest.raises(adapter.IdentityError):
            adapter.resolve_identity(request_get, target, "C3", 9606)
        entry[field] = original
    with pytest.raises(adapter.IdentityError):
        adapter.resolve_identity(request_get, "OTHER", "C3", 9606)
    before = len(queries)
    with pytest.raises(adapter.IdentityError):
        adapter.resolve_identity(request_get, 'EGFR OR organism_id:10090', 'C3', 9606)
    assert len(queries) == before
    for status in [400, 401, 403, 404, 422]:
        def rejected(url, **kwargs):
            return SimpleNamespace(status_code=status)
        with pytest.raises(adapter.QueryRejected, match=f'HTTP {status}'):
            adapter.resolve_identity(rejected, target, 'C3', 9606)


@pytest.mark.parametrize("reason", ["target_identity_mismatch", "retrieval_query_rejected"])
def test_identity_failure_is_actionable_and_not_retryable(tmp_path, reason):
    identifier = "hyp-" + "d" * 24
    output = folder(tmp_path, identifier)
    output.mkdir(parents=True)
    save(output / "request.json", {"target": "P00533", "site": "C797", "taxon": 9606})
    collector = tmp_path / "collector.py"
    collector.write_text("import json, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        f"(out / 'retrieval_failure.json').write_text(json.dumps({{'reason': {reason!r}, 'retryable': False, 'detail': 'No exact identity'}}))\n"
        "raise SystemExit(1)\n")
    result = run_collection(tmp_path, identifier, collector=collector)
    assert result["reason"] == reason
    assert result["retryable"] is False
    assert "unchanged inputs" in read(tmp_path, identifier)["next_action"]
    assert not (output / "manifest.json").exists()


def test_public_collector_reports_missing_external_adapter(tmp_path):
    identifier = "hyp-" + "e" * 24
    output = folder(tmp_path, identifier)
    output.mkdir(parents=True)
    save(output / "request.json", {"target": "P00533", "site": "C797", "taxon": 9606})
    collector = Path(__file__).resolve().parents[1] / "scripts/collect_hypothesis_evidence.py"

    result = run_collection(tmp_path, identifier, collector=collector)

    assert result["reason"] == "retrieval_adapter_not_distributed"
    assert result["retryable"] is False
    assert "separately licensed retrieval adapter" in read(tmp_path, identifier)["next_action"]
    assert not (output / "manifest.json").exists()


@pytest.fixture
def evidence(tmp_path):
    identifier = "hyp-" + "a" * 24
    output = folder(tmp_path, identifier)
    output.mkdir(parents=True)
    save(output / "evidence.json", {"target": "EGFR", "site": "C797", "taxon": 9606,
        "identity": {"uniprot_id": "P00533", "canonical_cys_verified": True},
        "literature": [{"id": "PMID:test", "title": "Synthetic unit-test source"}]})
    save(output / "manifest.json", {"files": {"evidence.json": digest(output / "evidence.json")}})
    return tmp_path, identifier


def proposal(route="library-first"):
    return {"route": route, "rationale": "Test hypothesis", "claims": [{
        "statement": "Test interpretation", "evidence_ids": ["PMID:test"], "interpretation": "inference"}],
        "warhead_class": ["acrylamide"], "uncertainties": ["No activity evidence"]}


def test_missing_registry_is_not_executable(evidence):
    root, identifier = evidence
    result = finalize(root, identifier, proposal())
    assert result["handoff"]["state"] == "NEEDS_INPUTS"
    assert result["handoff"]["intake"]["state"] == "TARGET_NOT_REGISTERED"
    assert result["handoff"]["next_call"] is None
    assert Path(result["artifacts"]["report"]).is_file()


def test_rejects_fabricated_citation(evidence):
    root, identifier = evidence
    data = proposal()
    data["claims"][0]["evidence_ids"] = ["PMID:invented"]
    with pytest.raises(ValueError, match="Unknown evidence"):
        finalize(root, identifier, data)


def test_ligand_parent_requires_binding_evidence(evidence):
    root, identifier = evidence
    data = proposal("ligand-first")
    data.update(parent_ligand="osimertinib", parent_binding_mode="covalent", parent_evidence_ids=["PMID:test"])
    result = finalize(root, identifier, data)
    assert result["handoff"]["state"] == "NEEDS_INPUTS"
    assert result["handoff"]["intake"]["missing"] == ["noncovalent_parent_binding_mode"]
    assert result["handoff"]["next_call"] is None
    assert result["handoff"]["compute_submitted"] is False
    assert result["campaign_registration"] is None
    assert Path(result["artifacts"]["hypothesis"]).is_file()


def test_missing_parent_publishes_blocked_proposal_without_default(evidence):
    root, identifier = evidence
    result = finalize(root, identifier, proposal("ligand-first"))
    assert result["handoff"]["state"] == "NEEDS_INPUTS"
    assert set(result["handoff"]["intake"]["missing"]) == {
        "parent_ligand", "noncovalent_parent_binding_mode", "parent_evidence_ids"}
    assert result["hypothesis"]["parent_ligand"] is None
    assert result["campaign_registration"] is None
    assert "route_plan" not in result["handoff"]
    assert result["handoff"]["next_call"] is None
    assert result["handoff"]["compute_submitted"] is False


def test_modified_evidence_rejected(evidence):
    root, identifier = evidence
    (folder(root, identifier) / "evidence.json").write_text("{}")
    with pytest.raises(ValueError, match="Evidence changed"):
        read(root, identifier)


def test_path_traversal_rejected(evidence):
    with pytest.raises(ValueError, match="Invalid hypothesis"):
        read(evidence[0], "../../etc")


@pytest.mark.parametrize("collector", ["raise SystemExit(7)", "pass"])
def test_failed_or_empty_collector_cannot_become_ready(tmp_path, collector):
    identifier = "hyp-" + "b" * 24
    output = folder(tmp_path, identifier)
    output.mkdir(parents=True)
    save(output / "request.json", {"target": "EGFR", "site": "C797", "taxon": 9606})
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "collect_hypothesis_evidence.py").write_text(collector)
    # Real subprocess execution: a nonzero exit, or a zero exit without evidence,
    # must both remain failed rather than publishing an empty successful report.
    result = run_collection(tmp_path, identifier, collector=scripts / "collect_hypothesis_evidence.py")
    assert result["state"] == "FAILED"
    assert read(tmp_path, identifier)["state"] == "FAILED"
    assert not (output / "manifest.json").exists()
    with pytest.raises(ValueError, match="Evidence is not ready"):
        finalize(tmp_path, identifier, proposal())


def test_failed_status_does_not_require_live_worker(evidence):
    root, identifier = evidence
    failed = "hyp-" + "c" * 24
    output = folder(root, failed)
    output.mkdir()
    save(output / "status.json", {"state": "FAILED", "reason": "retrieval_timeout"})
    assert read(root, failed) == {"evidence_id": failed, "state": "FAILED", "reason": "retrieval_timeout"}


def test_ligand_handoff_requires_comparison(evidence):
    root, identifier = evidence
    data = proposal("ligand-first")
    data.update(parent_ligand="erlotinib", parent_binding_mode="noncovalent", parent_evidence_ids=["PMID:test"])
    result = finalize(root, identifier, data)
    assert result["handoff"]["intake"]["intent"] == "ligand-first"
    assert result["handoff"]["state"] == "NEEDS_PARENT_COMPARISON"
    assert result["handoff"]["compute_submitted"] is False


def test_ready_library_compiles_actual_next_request(evidence):
    from test_library_intake import _ready_root
    root, identifier = evidence
    _ready_root(root)
    result = finalize(root, identifier, proposal(), scope="pilot")
    assert result["handoff"]["state"] == "READY"
    assert result["handoff"]["route_plan"]["next_request"]["engine"] == "covindb-prescreen"
    assert result["handoff"]["route_plan"]["submitted"] is False


def test_platform_library_is_explicit_and_missing_path_blocks(evidence, monkeypatch):
    root, identifier = evidence
    configs = root / "configs"
    configs.mkdir()
    profile = {"route": "library-first", "target": "EGFR", "site": "C797", "taxon": 9606,
        "uniprot_id": "P00533", "tool": "inspire_screening_task", "target_parameters": {"target": "EGFR"}}
    registry = configs / "target_hypothesis_routes.json"
    registry.write_text(json.dumps({"profiles": [profile]}))
    monkeypatch.setenv("TARGET_HYPOTHESIS_PLATFORM_TOOLS", "inspire_screening_task")
    missing = finalize(root, identifier, proposal())
    assert missing["handoff"]["state"] == "NEEDS_INPUTS"
    assert missing["handoff"]["next_call"] is None
    for filename in ["libraries/custom-one.csv", "libraries/custom-two.csv"]:
        profile["library"] = filename
        registry.write_text(json.dumps({"profiles": [profile]}))
        planned = finalize(root, identifier, proposal())
        assert planned["handoff"]["next_call"]["arguments"]["library"] == filename
        assert planned["handoff"]["compute_submitted"] is False


def test_unsupported_warhead_cannot_use_ready_acrylamide_campaign(evidence):
    from test_library_intake import _ready_root
    root, identifier = evidence
    _ready_root(root)
    data = proposal()
    data["warhead_class"] = ["chloroacetamide"]
    result = finalize(root, identifier, data, scope="pilot")
    assert result["handoff"]["state"] == "NEEDS_INPUTS"
    assert result["handoff"]["next_call"] is None
    assert result["handoff"]["compatibility_blockers"]


def test_registered_ligand_cannot_bypass_parent_comparison(evidence):
    import yaml
    root, identifier = evidence
    config = root / "configs/ligand_first"
    config.mkdir(parents=True)
    (root / "protein.pdb").write_text("TEST FIXTURE ONLY")
    save(root / "campaign.json", {"allowed_warheads": ["acrylamide"], "target": {"symbol": "EGFR", "uniprot_id": "P00533", "organism": "Homo sapiens",
        "nucleophile": {"uniprot_residue_number": 797, "residue_name": "CYS", "atom_name": "SG"}},
        "parent_pose": {"ligand_name": "erlotinib", "pdb_id": "1M17", "ligand_component_id": "AQ4"}})
    (config / "test.yaml").write_text(yaml.safe_dump({"campaign_id": "test-egfr", "project_id": "egfr-c797",
        "campaign_path": "campaign.json", "trace_chain_config": "trace.yaml", "target_config": "target.yaml",
        "pdb": "protein.pdb", "reference_ligand": "parent.sdf", "af3_source_data_json": "seed.json",
        "covdock_receptor": "receptor.mae", "attachment_residue": "A:797", "covres": "A:CYS:797",
        "seed_run_ids": {}}))
    data = proposal("ligand-first")
    data.update(parent_ligand="erlotinib", parent_binding_mode="noncovalent", parent_evidence_ids=["PMID:test"])
    result = finalize(root, identifier, data)
    assert result["handoff"]["state"] == "NEEDS_PARENT_COMPARISON"
    assert result["handoff"]["next_call"] is None
    assert result["campaign_registration"] is None
    assert result["handoff"]["compute_submitted"] is False
    assert yaml.safe_load((config / "test.yaml").read_text())["project_id"] == "egfr-c797"
