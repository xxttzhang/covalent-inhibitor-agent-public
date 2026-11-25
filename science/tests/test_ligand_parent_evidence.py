import copy
import json
from pathlib import Path

import pytest

from covscreen.ligand_parent_evidence import (
    BindingEvidence, ParentComparison, branch, normalize, seal, source_spec,
    validate_comparison, verify_binding, mechanism_supported,
)
from covscreen.target_hypothesis import read, digest, finalize


@pytest.mark.parametrize('acronym', ['EGFR-TKIs', 'EGFR-TKls'])
@pytest.mark.parametrize('parent', ['gefitinib', 'erlotinib'])
def test_target_qualified_tki_class_attribution(acronym, parent):
    quote = f'First generation reversible ATP-competitive {acronym}, gefitinib and erlotinib, are effective as first-line therapy.'
    assert mechanism_supported({'title': parent}, quote, {'abstract': quote}, target='EGFR')
    assert not mechanism_supported({'title': 'afatinib'}, quote, {'abstract': quote}, target='EGFR')
    assert not mechanism_supported({'title': parent}, quote, {'abstract': quote}, target='HER2')
    for changed in (quote.replace('reversible', 'irreversible'),
                    quote.replace('reversible', 'not reversible'),
                    quote.replace('reversible', 'possibly reversible')):
        assert not mechanism_supported({'title': parent}, changed, {'abstract': changed}, target='EGFR')


def dataset():
    records, geometry, candidates = {}, {}, []
    for i, name in enumerate(("ParentAlpha", "ParentBeta"), 1):
        molecule = f"ChEMBL:CHEMBL{i}"
        source = f"PMID:{i}"
        quote = f"{name} is a reversible inhibitor of the TEST kinase in a biochemical assay."
        records[molecule] = {"kind": "molecule", "title": name, "synonyms": [], "inchikey": f"fixture-key-{i}",
                             "computed_properties": {"fragments": 1}}
        records[source] = {"kind": "literature", "title": "Synthetic validation fixture", "abstract": quote}
        geometry[f"geometry-{i}"] = {"molecule_id": molecule,
            "chemical_identity": {"status": "declared_parent_identity_and_heavy_atoms_verified", "expected_inchikey": f"fixture-key-{i}", "structure_sha256": f"fixture-hash-{i}"},
            "coordinate_audit": {"status": "coordinate_checks_passed_linkage_review_pending", "protein_author_chain": "A", "uniprot": "TEST01", "uniprot_residue": 1, "structure_sha256": f"fixture-hash-{i}"},
            "deposited_proteins": [{"uniprot_ids": ["TEST01"], "author_chains": ["A"], "mutation_count": 0, "organisms": [{"ncbi_taxonomy_id": 9606}]}]}
        candidates.append({"molecule_id": molecule,
            "disposition": "priority_for_geometry" if i == 1 else "backup",
            "binding_evidence": [{"source_id": source, "quote": quote}],
            "geometry_id": f"geometry-{i}", "rationale": "Fixture, not scientific evidence",
            "uncertainties": ["Exit vector remains unknown"]})
    return {"target": "TEST", "taxon": 9606, "site": "C1", "identity": {"uniprot_id": "TEST01", "canonical_cys_verified": True},
        "literature": [], "supplemental_sources": records, "parent_geometry": geometry}, {
        "candidates": candidates, "selected_molecule_id": "ChEMBL:CHEMBL1", "scope": "Synthetic fixture only", "target_state": "wild_type"}


def test_valid_comparison_does_not_admit_derivatization_or_compute():
    evidence, proposal = dataset()
    result = validate_comparison(evidence, proposal, "ParentAlpha")
    assert result["state"] == "PARENT_COMPARISON_REVIEW_READY"
    assert result["execution_ready"] is False
    assert result["derivatization_feasibility"] == "unknown"


def test_pdb_source_id_is_not_geometry_and_existing_record_is_discoverable():
    evidence, proposal = dataset()
    proposal["candidates"][0]["geometry_id"] = "PDB:1ABC"
    result = validate_comparison(evidence, proposal, "ParentAlpha")
    assert result["state"] == "NEEDS_PARENT_EVIDENCE"
    row = result["candidates"][0]
    assert any(reason.startswith("unknown_geometry_record_id:") for reason in row["blockers"])
    assert row["available_geometry_ids"] == ["geometry-1"]
    assert proposal["candidates"][0]["geometry_id"] == "PDB:1ABC"


@pytest.mark.parametrize("mutation", ["quote", "source", "identity", "geometry", "linkage", "parent", "missing_geometry"])
def test_independent_evidence_gates_reject_invalid_parent(mutation):
    evidence, proposal = dataset()
    row = proposal["candidates"][0]
    parent = "ParentAlpha"
    if mutation == "quote": row["binding_evidence"][0]["quote"] = "ParentAlpha is a reversible drug, invented source quotation."
    if mutation == "source": row["binding_evidence"][0]["source_id"] = "PMID:999"
    if mutation == "identity": evidence["supplemental_sources"][row["molecule_id"]]["computed_properties"]["fragments"] = 2
    if mutation == "geometry": row["geometry_id"] = "geometry-2"
    if mutation == "linkage": evidence["parent_geometry"]["geometry-1"]["coordinate_audit"]["status"] = "explicit_linkage_requires_review"
    if mutation == "parent": parent = "UnrelatedDrug"
    if mutation == "missing_geometry": evidence["parent_geometry"]["geometry-1"]["chemical_identity"] = None
    assert validate_comparison(evidence, proposal, parent)["state"] == "NEEDS_PARENT_EVIDENCE"


@pytest.mark.parametrize("quote", ["ParentAlpha is an irreversible inhibitor with no reversible action.",
    "ParentAlpha is not reversible in this binding experiment.",
    "ParentBeta is a reversible kinase inhibitor in this assay."])
def test_keyword_or_other_drug_cannot_establish_parent_binding(quote):
    record = {"title": "ParentAlpha", "synonyms": []}
    sources = {"PMID:1": {"kind": "literature", "title": "", "abstract": quote}}
    assert verify_binding(record, [BindingEvidence(source_id="PMID:1", quote=quote)], sources)


def test_model_cannot_supply_structural_facts_instead_of_geometry():
    evidence, proposal = dataset()
    proposal["candidates"][0]["resolution"] = 1.85
    with pytest.raises(ValueError): ParentComparison.model_validate(proposal)


def test_supplement_version_preserves_original_and_detects_tampering(tmp_path):
    evidence, _ = dataset()
    identifier = "hyp-" + "a" * 24
    folder = tmp_path / "results/target-hypotheses" / identifier
    folder.mkdir(parents=True)
    (folder / "raw.json").write_text('{"actual":"raw fixture"}')
    seal(identifier, folder, evidence)
    before = digest(folder / "manifest.json")
    child, out, data = branch(tmp_path, identifier)
    data["supplemental_sources"]["PMID:3"] = {"kind": "literature", "title": "Additional fixture"}
    seal(child, out, data)
    assert digest(folder / "manifest.json") == before
    assert "PMID:3" not in read(tmp_path, identifier)["source_index"]
    assert "PMID:3" in read(tmp_path, child)["source_index"]
    (out / "raw.json").write_text("tampered")
    with pytest.raises(ValueError, match="Evidence changed"): read(tmp_path, child)


@pytest.mark.parametrize("identifier", ["file:///etc/passwd", "https://arbitrary.invalid", "PMID:1/../2", "PDB:../../", "ChEMBL:CHEMBL1?url=internal"])
def test_supplement_rejects_paths_urls_and_identifier_injection(identifier):
    with pytest.raises(ValueError): source_spec(identifier)


def test_actual_pdb_metadata_not_model_memory():
    raw = {"rcsb_id": "3POZ", "struct": {"title": "EGFR Kinase domain complexed with tak-285"},
           "rcsb_entry_info": {"resolution_combined": [1.5]}}
    result = normalize("PDB:3POZ", "pdb", raw)
    assert result["title"] == raw["struct"]["title"]
    assert result["resolution_angstrom"] == [1.5]
    assert result["conformation"] == "not_assessed"
    with pytest.raises(ValueError): normalize("PDB:1XKK", "pdb", raw)


def test_unverified_exclusion_cannot_change_parent_comparison():
    evidence, proposal = dataset()
    proposal['candidates'][1]['disposition'] = 'excluded_covalent'
    result = validate_comparison(evidence, proposal, 'ParentAlpha')
    assert any('covalent_exclusion_requires' in item for item in result['blockers'])


def test_read_sources_returns_complete_record_without_file_paths(tmp_path):
    from covscreen.ligand_parent_evidence import read_sources
    evidence, _ = dataset()
    identifier = 'hyp-' + 'b' * 24
    folder = tmp_path / 'results/target-hypotheses' / identifier
    folder.mkdir(parents=True)
    evidence['supplemental_sources']['PMID:1']['abstract'] = 'A' * 1200 + ' ParentAlpha is reversible.'
    summary = seal(identifier, folder, evidence)
    assert summary['source_index']['PMID:1']['complete_abstract_available']
    detail = read_sources(tmp_path, identifier, ['PMID:1'])
    assert detail['sources']['PMID:1']['abstract'].endswith('ParentAlpha is reversible.')
    with pytest.raises(ValueError, match='Unknown source'):
        read_sources(tmp_path, identifier, ['PMID:999'])


def test_stale_parent_cannot_silently_fork_supplemental_evidence(tmp_path):
    evidence, _ = dataset()
    identifier = 'hyp-' + 'c' * 24
    folder = tmp_path / 'results/target-hypotheses' / identifier
    folder.mkdir(parents=True)
    seal(identifier, folder, evidence)
    first, out1, data1 = branch(tmp_path, identifier)
    second, out2, data2 = branch(tmp_path, identifier)
    seal(first, out1, data1)
    with pytest.raises(ValueError, match='STALE_EVIDENCE_VERSION'):
        seal(second, out2, data2)
    with pytest.raises(ValueError, match=first):
        branch(tmp_path, identifier)
    assert read(tmp_path, identifier)['state'] == 'EVIDENCE_READY'


def test_compact_response_bounds_realistic_nested_metadata():
    from covscreen.ligand_parent_evidence import compact
    evidence, _ = dataset()
    for i in range(50):
        evidence['supplemental_sources'][f'PMID:{100+i}'] = {'kind':'literature','title':'T'*150,'abstract':'X'*30000}
    evidence['supplemental_sources']['PDB:1ABC'] = {'kind':'pdb','title':'Structure','proteins':[
        {'entity_id':'1','organisms':[{'scientific_name':'Homo sapiens','ncbi_taxonomy_id':9606,'ncbi_parent_scientific_name':'X'*90000}]}]}
    output = compact('hyp-fixture', evidence)
    assert len(json.dumps(output,ensure_ascii=False,indent=2).encode()) < 30000
    assert 'ncbi_parent_scientific_name' not in json.dumps(output)
    assert len(output['source_index']) == len(evidence['supplemental_sources'])


@pytest.mark.parametrize("field,value", [("mutation_count", 3), ("mutation_count", None), ("author_chains", ["B"]), ("organisms", [{"ncbi_taxonomy_id": 10090}])])
def test_selected_structure_scope_cannot_be_inferred_from_title(field, value):
    evidence, proposal = dataset()
    evidence["parent_geometry"]["geometry-1"]["deposited_proteins"][0][field] = value
    assert validate_comparison(evidence, proposal, "ParentAlpha")["state"] == "NEEDS_PARENT_EVIDENCE"


def test_mutant_scope_requires_all_deposited_substitutions():
    evidence, proposal = dataset()
    proposal.update(target_state="specified_mutant", expected_mutations=["T790M"])
    for geometry in evidence["parent_geometry"].values():
        geometry["deposited_proteins"][0].update(mutation_count=2, mutations="T790M, L858R")
    assert validate_comparison(evidence, proposal, "ParentAlpha")["state"] == "NEEDS_PARENT_EVIDENCE"
    proposal["expected_mutations"] = ["T790M", "L858R"]
    assert validate_comparison(evidence, proposal, "ParentAlpha")["state"] == "PARENT_COMPARISON_REVIEW_READY"


def test_abandoned_branch_cannot_be_read_or_used(tmp_path):
    evidence, _ = dataset()
    identifier = "hyp-" + "d" * 24
    folder = tmp_path / "results/target-hypotheses" / identifier
    folder.mkdir(parents=True)
    seal(identifier, folder, evidence)
    first, out1, data1 = branch(tmp_path, identifier)
    second, out2, data2 = branch(tmp_path, identifier)
    seal(first, out1, data1)
    with pytest.raises(ValueError, match="STALE"):
        seal(second, out2, data2)
    with pytest.raises(ValueError, match="UNCOMMITTED"):
        read(tmp_path, second)
    with pytest.raises(ValueError, match="UNCOMMITTED"):
        branch(tmp_path, second)
    assert read(tmp_path, first)["state"] == "EVIDENCE_READY"


def test_old_version_cannot_finalize_ligand_proposal(tmp_path):
    evidence, comparison = dataset()
    identifier = "hyp-" + "e" * 24
    folder = tmp_path / "results/target-hypotheses" / identifier
    folder.mkdir(parents=True)
    seal(identifier, folder, evidence)
    child, out, data = branch(tmp_path, identifier)
    seal(child, out, data)
    hypothesis = {"route": "ligand-first", "rationale": "Test", "claims": [{"statement": "Test", "evidence_ids": ["PMID:1"], "interpretation": "inference"}], "warhead_class": ["acrylamide"], "uncertainties": ["Unknown"], "parent_ligand": "ParentAlpha", "parent_binding_mode": "noncovalent", "parent_evidence_ids": ["PMID:1"], "parent_comparison": comparison}
    with pytest.raises(ValueError, match="STALE"):
        finalize(tmp_path, identifier, hypothesis)


@pytest.mark.parametrize("abstract,quote", [
    ("ParentAlpha is irreversible. ParentBeta is reversible.", "ParentAlpha is irreversible. ParentBeta is reversible."),
    ("It is unknown whether ParentAlpha is reversible in this assay.", "ParentAlpha is reversible in this assay."),
    ("ParentAlpha is not a reversible inhibitor of this kinase.", "a reversible inhibitor of this kinase."),
    ("ParentAlpha is irreversible whereas ParentBeta is reversible.", "ParentAlpha is irreversible whereas ParentBeta is reversible."),
    ("ParentAlphaDerivative is a reversible kinase inhibitor.", "ParentAlphaDerivative is a reversible kinase inhibitor."),
])
def test_mechanism_requires_same_complete_unambiguous_clause(abstract, quote):
    source = {"kind": "literature", "abstract": abstract}
    assert verify_binding({"title": "ParentAlpha"}, [BindingEvidence(source_id="PMID:1", quote=quote)], {"PMID:1": source})


@pytest.mark.parametrize("field,value", [("uniprot", "OTHER"), ("uniprot_residue", 9), ("structure_sha256", "other-file")])
def test_geometry_labels_cannot_override_actual_provenance(field, value):
    evidence, proposal = dataset()
    evidence["parent_geometry"]["geometry-1"]["coordinate_audit"][field] = value
    result = validate_comparison(evidence, proposal, "ParentAlpha")
    assert any("geometry_target_site_or_parent_provenance_mismatch" in x for x in result["blockers"])


def test_large_measurement_history_is_bounded_but_details_remain_readable(tmp_path):
    from covscreen.ligand_parent_evidence import read_sources
    evidence, _ = dataset()
    for i in range(150):
        evidence["parent_geometry"][f"geometry-large-{i}"] = {"id": f"geometry-large-{i}", "mapping": ["X" * 2000], "status": "failed"}
    identifier = "hyp-" + "f" * 24
    folder = tmp_path / "results/target-hypotheses" / identifier
    folder.mkdir(parents=True)
    summary = seal(identifier, folder, evidence)
    assert len(json.dumps(summary, ensure_ascii=False, indent=2).encode()) < 30000
    request = summary['parent_geometry']['geometry-large-149']['details_request']
    assert request == {'action': 'read_sources', 'source_ids': ['geometry-large-149']}
    assert read_sources(tmp_path, identifier, request['source_ids'])['sources']['geometry-large-149']['mapping'] == ['X' * 2000]
    detailed = read_sources(tmp_path, identifier, ["geometry-large-0"])
    assert detailed["sources"]["geometry-large-0"]["mapping"] == ["X" * 2000]


def test_simultaneous_commit_has_exactly_one_readable_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    evidence, _ = dataset()
    identifier = "hyp-" + "1" * 24
    folder = tmp_path / "results/target-hypotheses" / identifier
    folder.mkdir(parents=True)
    seal(identifier, folder, evidence)
    branches = [branch(tmp_path, identifier), branch(tmp_path, identifier)]
    barrier = Barrier(2)
    def commit(parts):
        barrier.wait()
        try:
            seal(*parts)
            return parts[0], True
        except ValueError as exc:
            assert "STALE" in str(exc)
            return parts[0], False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, branches))
    assert sum(success for _, success in results) == 1
    for child, success in results:
        if success:
            assert read(tmp_path, child)["state"] == "EVIDENCE_READY"
        else:
            with pytest.raises(ValueError, match="UNCOMMITTED"):
                read(tmp_path, child)


def test_unverified_canonical_cys_cannot_pass_comparison():
    evidence, proposal = dataset()
    evidence["identity"]["canonical_cys_verified"] = False
    assert "canonical_target_cys_verification_required" in validate_comparison(evidence, proposal, "ParentAlpha")["blockers"]


@pytest.mark.parametrize("quote", [
    "Compared with reversible inhibitor ParentBeta, ParentAlpha showed activity.",
    "ParentAlpha was inactive and ParentBeta was reversible in the assay.",
    "The reversible kinase inhibitor ParentBeta was compared with ParentAlpha.",
])
def test_mechanism_of_same_sentence_comparator_is_not_assigned_to_parent(quote):
    assert verify_binding({"title": "ParentAlpha"}, [BindingEvidence(source_id="PMID:1", quote=quote)],
        {"PMID:1": {"kind": "literature", "abstract": quote}})


@pytest.mark.parametrize("quote", [
    "ParentAlpha is a reversible kinase inhibitor.",
    "ParentAlpha, a non-covalent EGFR inhibitor, was evaluated.",
    "The reversible EGFR tyrosine kinase inhibitor ParentAlpha was evaluated.",
])
def test_explicit_parent_mechanism_remains_accepted(quote):
    assert not verify_binding({"title": "ParentAlpha"}, [BindingEvidence(source_id="PMID:1", quote=quote)],
        {"PMID:1": {"kind": "literature", "abstract": quote}})


def test_commit_waits_for_version_publication_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from covscreen.ligand_parent_evidence import version_lock
    evidence, _ = dataset()
    identifier = "hyp-" + "2" * 24
    folder = tmp_path / "results/target-hypotheses" / identifier
    folder.mkdir(parents=True)
    seal(identifier, folder, evidence)
    child = branch(tmp_path, identifier)
    started = Event()
    def commit():
        started.set()
        return seal(*child)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with version_lock(folder):
            future = pool.submit(commit)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
            assert not (folder / "successor.json").exists()
        assert future.result(timeout=3)["evidence_id"] == child[0]
    assert read(tmp_path, child[0])["state"] == "EVIDENCE_READY"


def test_two_database_ids_for_same_parent_do_not_satisfy_comparison():
    evidence, proposal = dataset()
    evidence["supplemental_sources"]["ChEMBL:CHEMBL2"]["inchikey"] = "fixture-key-1"
    evidence["parent_geometry"]["geometry-2"]["chemical_identity"]["expected_inchikey"] = "fixture-key-1"
    result = validate_comparison(evidence, proposal, "ParentAlpha")
    assert "comparison_requires_chemically_distinct_parents_not_duplicate_database_ids" in result["blockers"]


def test_valid_exclusion_quote_does_not_hide_fabricated_second_quote():
    evidence, proposal = dataset()
    row = proposal["candidates"][1]
    row["disposition"] = "excluded_covalent"
    quote = "ParentBeta is an irreversible inhibitor of this kinase."
    evidence["supplemental_sources"]["PMID:2"]["abstract"] = quote
    row["binding_evidence"] = [{"source_id": "PMID:2", "quote": quote},
        {"source_id": "PMID:1", "quote": "ParentBeta is a covalent drug in a fabricated quotation."}]
    result = validate_comparison(evidence, proposal, "ParentAlpha")
    assert any("invalid_covalent_exclusion_citation" in x for x in result["blockers"])


@pytest.mark.parametrize("phrase,covalent", [("non-reversible", False), ("non reversible", False), ("non-irreversible", True), ("non irreversible", True)])
def test_negated_mechanism_prefix_cannot_validate_opposite_mode(phrase, covalent):
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = f"ParentAlpha is a {phrase} inhibitor of the kinase."
    assert not mechanism_supported({"title": "ParentAlpha"}, quote, {"abstract": quote}, covalent=covalent)


def test_lineage_queue_preserves_both_concurrent_additions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from covscreen.ligand_parent_evidence import append_version, latest_version
    evidence, _ = dataset()
    identifier = "hyp-" + "3" * 24
    folder = tmp_path / "results/target-hypotheses" / identifier
    folder.mkdir(parents=True)
    seal(identifier, folder, evidence)
    barrier = Barrier(2)
    def append(number):
        barrier.wait()
        with append_version(tmp_path, identifier) as base:
            child, output, data = branch(tmp_path, base)
            data['supplemental_sources'][f'PMID:{number}'] = {'kind': 'literature', 'title': f'Addition {number}'}
            return base, seal(child, output, data)['evidence_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, [101, 102]))
    assert sum(base == identifier for base, _ in results) == 1
    latest = latest_version(tmp_path, identifier)
    records = read(tmp_path, latest)['source_index']
    assert {'PMID:101', 'PMID:102'}.issubset(records)
    for _, child in results:
        assert read(tmp_path, child)['state'] == 'EVIDENCE_READY'


def test_append_queue_does_not_admit_abandoned_branch(tmp_path):
    from covscreen.ligand_parent_evidence import append_version
    evidence, _ = dataset()
    identifier = "hyp-" + "4" * 24
    folder = tmp_path / "results/target-hypotheses" / identifier
    folder.mkdir(parents=True)
    seal(identifier, folder, evidence)
    first = branch(tmp_path, identifier)
    loser = branch(tmp_path, identifier)
    seal(*first)
    with pytest.raises(ValueError, match="STALE"):
        seal(*loser)
    with pytest.raises(ValueError, match="UNCOMMITTED"):
        with append_version(tmp_path, loser[0]):
            pytest.fail('An abandoned branch cannot be rebased')


def test_public_tool_queues_stale_additions_and_reports_actual_base(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from covscreen.target_hypothesis import hypothesis_tool
    from covscreen.ligand_parent_evidence import latest_version
    evidence, _ = dataset()
    identifier = "hyp-" + "5" * 24
    folder = tmp_path / "results/target-hypotheses" / identifier
    folder.mkdir(parents=True)
    seal(identifier, folder, evidence)
    barrier = Barrier(2)
    def call(source):
        barrier.wait()
        # Exact sources are already sealed. This uses the real public action
        # without a remote fetch, mocks, or mutation of the source payload.
        return hypothesis_tool(tmp_path, action='supplement', evidence_id=identifier, source_ids=[source])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, ['ChEMBL:CHEMBL1', 'ChEMBL:CHEMBL2']))
    rebased = [x for x in results if 'automatic_append_rebase' in x]
    assert len(rebased) == 1
    actual = rebased[0]['automatic_append_rebase']
    assert actual['requested_evidence_id'] == identifier
    first = next(x for x in results if 'automatic_append_rebase' not in x)
    assert actual['base_evidence_id'] == first['evidence_id']
    assert latest_version(tmp_path, identifier) == rebased[0]['evidence_id']
    assert read(tmp_path, identifier)['source_index'] == read(tmp_path, rebased[0]['evidence_id'])['source_index']


def test_source_reader_does_not_expose_backend_paths_or_claim_article_text(tmp_path):
    from covscreen.ligand_parent_evidence import read_sources
    evidence, _ = dataset()
    evidence['supplemental_sources']['PMID:1']['raw_path'] = 'literature_backend_storage.json'
    identifier = 'hyp-' + '6' * 24
    folder = tmp_path / 'results/target-hypotheses' / identifier
    folder.mkdir(parents=True)
    summary = seal(identifier, folder, evidence)
    assert 'full_text_available' not in json.dumps(summary)
    detail = read_sources(tmp_path, identifier, ['PMID:1'])
    assert 'raw_path' not in detail['sources']['PMID:1']
    assert 'not article full text' in detail['content_scope']
    assert read(tmp_path, identifier)['evidence']['supplemental_sources']['PMID:1']['raw_path'] == 'literature_backend_storage.json'


def test_parent_mechanism_query_uses_verified_identity_and_abstract_scope():
    from covscreen.ligand_parent_evidence import parent_mechanism_query
    evidence, _ = dataset()
    query = parent_mechanism_query(evidence, "ChEMBL:CHEMBL1")
    assert query.startswith('ABSTRACT:(')
    assert '"ParentAlpha"' in query and '"TEST"' in query
    assert '(reversible OR noncovalent OR "non-covalent")' in query
    with pytest.raises(ValueError, match="Supplement"):
        parent_mechanism_query(evidence, "ChEMBL:CHEMBL999")


def test_binding_to_another_target_does_not_validate_requested_target():
    evidence, proposal = dataset()
    quote = "ParentAlpha is a reversible inhibitor of OTHER kinase."
    evidence['supplemental_sources']['PMID:1']['abstract'] = quote
    proposal['candidates'][0]['binding_evidence'][0]['quote'] = quote
    result = validate_comparison(evidence, proposal, 'ParentAlpha')
    assert result['state'] == 'NEEDS_PARENT_EVIDENCE'
    assert any('quote_must_explicitly_link_parent_target' in x for x in result['blockers'])


@pytest.mark.parametrize("name", ["ParentAlpha", "ParentBeta"])
def test_explicit_coordinated_parent_list_supports_each_named_member(name):
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = "First-generation reversible TEST inhibitors, such as ParentAlpha and ParentBeta, remain useful."
    assert mechanism_supported({'title': name}, quote, {'abstract': quote}, target='TEST')


@pytest.mark.parametrize('invalid', [False, True])
def test_finalization_surfaces_gate_before_large_sources_and_preserves_full_artifact(tmp_path, invalid):
    evidence, comparison = dataset()
    evidence['supplemental_sources']['PMID:1']['large_metadata'] = 'X' * 60000
    if invalid:
        comparison['candidates'][0]['binding_evidence'][0]['quote'] = 'ParentAlpha is reversible according to a fabricated source.'
    identifier = 'hyp-' + '7' * 24
    folder = tmp_path / 'results/target-hypotheses' / identifier
    folder.mkdir(parents=True)
    seal(identifier, folder, evidence)
    hypothesis = {'route':'ligand-first', 'rationale':'Scientific test hypothesis',
        'claims':[{'statement':'Test', 'evidence_ids':['PMID:1'], 'interpretation':'inference'}],
        'warhead_class':['acrylamide'], 'uncertainties':['Not proven'], 'parent_ligand':'ParentAlpha',
        'parent_binding_mode':'noncovalent', 'parent_evidence_ids':['PMID:1'], 'parent_comparison':comparison}
    result = finalize(tmp_path, identifier, hypothesis)
    assert result['state'] == ('NEEDS_PARENT_EVIDENCE' if invalid else 'PARENT_COMPARISON_REVIEW_READY')
    assert list(result)[0] == 'state'
    assert len(json.dumps(result,ensure_ascii=False,indent=2).encode()) < 12000
    saved = json.loads(Path(result['artifacts']['hypothesis']).read_text())
    for key, expected in comparison.items():
        assert saved['hypothesis']['parent_comparison'][key] == expected
    assert saved['hypothesis']['rationale'] == hypothesis['rationale']
    assert any(x.get('large_metadata') == 'X' * 60000 for x in saved['references'])
    assert Path(result['artifacts']['report']).read_text().startswith('验证状态：**' + result['state'])
    assert result['handoff']['compute_submitted'] is False


def test_valid_mechanism_excerpt_does_not_require_unrelated_following_metadata():
    quote = "ParentAlpha is a reversible inhibitor of TEST"
    source = {'kind':'literature', 'abstract': quote + ', which was evaluated at multiple concentrations.'}
    assert not verify_binding({'title':'ParentAlpha'}, [BindingEvidence(source_id='PMID:1',quote=quote)], {'PMID:1':source}, target='TEST')


@pytest.mark.parametrize('context', [
    'It is unknown whether ParentAlpha is a reversible inhibitor of TEST.',
    'ParentAlpha is a reversible inhibitor of TEST, which was later disproved.',
    'The statement that ParentAlpha is a reversible inhibitor of TEST is incorrect.',
])
def test_excerpt_cannot_hide_source_context_qualifications(context):
    quote = 'ParentAlpha is a reversible inhibitor of TEST'
    assert verify_binding({'title':'ParentAlpha'}, [BindingEvidence(source_id='PMID:1',quote=quote)],
        {'PMID:1':{'kind':'literature','abstract':context}}, target='TEST')


def test_parenthetical_alias_and_same_subject_function_are_supported():
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = 'ParentAlpha (PA-123) is a small molecule that is administered orally and functions as a reversible inhibitor of both TEST and OTHER kinases.'
    assert mechanism_supported({'title':'ParentAlpha','synonyms':['PA-123']}, quote, {'abstract':quote}, target='TEST')


def test_parenthetical_alias_does_not_transfer_other_subject_mechanism():
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = 'ParentAlpha (PA-123) is inactive and ParentBeta functions as a reversible inhibitor of TEST.'
    assert not mechanism_supported({'title':'ParentAlpha','synonyms':['PA-123']}, quote, {'abstract':quote}, target='TEST')


@pytest.mark.parametrize('description', ['oral', 'orally administered', 'orally available', 'orally active', 'orally bioavailable'])
def test_coordinated_administration_adjective_keeps_same_subject(description):
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = f'ParentAlpha is an {description} and reversible TEST kinase inhibitor.'
    assert mechanism_supported({'title':'ParentAlpha'}, quote, {'abstract':quote}, target='TEST')


def test_administration_adjective_does_not_hide_new_subject():
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = 'ParentAlpha is oral and ParentBeta is a reversible TEST inhibitor.'
    assert not mechanism_supported({'title':'ParentAlpha'}, quote, {'abstract':quote}, target='TEST')


@pytest.mark.parametrize("name,covalent", [
    ("ParentAlpha", False), ("ParentBeta", False), ("ParentGamma", True),
])
def test_explicit_target_classes_keep_each_members_mechanism(name, covalent):
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = ("Multiple iterations of TEST TKIs, spanning from first-generation reversible "
             "inhibitors such as ParentAlpha and ParentBeta to the current standard-of-care "
             "third-generation covalent inhibitor ParentGamma, have been studied.")
    source = {"abstract": quote}
    assert mechanism_supported({"title": name}, quote, source, target="TEST", covalent=covalent)
    assert not mechanism_supported({"title": name}, quote, source, target="TEST", covalent=not covalent)
    assert not mechanism_supported({"title": name}, quote, source, target="OTHER", covalent=covalent)


@pytest.mark.parametrize("prefix", ["It is unknown whether ", "It is incorrect that "])
def test_target_class_excerpt_cannot_remove_enclosing_uncertainty(prefix):
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = ("TEST inhibitors, spanning from reversible inhibitors ParentAlpha and ParentBeta "
             "to covalent inhibitor ParentGamma, are effective")
    assert not mechanism_supported({"title": "ParentBeta"}, quote,
                                   {"abstract": prefix + quote + "."}, target="TEST")


def test_same_parent_in_both_contrasted_classes_remains_ambiguous():
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = ("TEST TKIs, spanning from reversible inhibitor ParentAlpha "
             "to covalent inhibitor ParentAlpha, were discussed.")
    for covalent in (False, True):
        assert not mechanism_supported({"title": "ParentAlpha"}, quote, {"abstract": quote},
                                       target="TEST", covalent=covalent)


@pytest.mark.parametrize("target_name", ["EGFR", "epidermal growth factor receptor"])
@pytest.mark.parametrize("adjectives", ["", "highly specific and ", "selective and "])
def test_explicit_target_name_and_same_subject_adjectives(target_name, adjectives):
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = f"Erlotinib is a {adjectives}reversible {target_name} tyrosine kinase inhibitor."
    assert mechanism_supported({"title": "ERLOTINIB"}, quote, {"abstract": quote}, target="EGFR")
    assert not mechanism_supported({"title": "ERLOTINIB"}, quote, {"abstract": quote}, target="HER2")
    assert not mechanism_supported({"title": "ERLOTINIB"}, quote, {"abstract": quote}, target="EGFR", covalent=True)


def test_retrieved_erlotinib_abstract_mechanism_keeps_exact_quote_check():
    quote = ("Erlotinib is a highly specific and reversible epidermal growth factor receptor "
             "tyrosine kinase inhibitor for the targeted therapy of non-small-cell lung cancer (NSCLC)")
    source = {"kind": "literature", "abstract": quote}
    citation = BindingEvidence(source_id="PMID:36034879", quote=quote)
    assert verify_binding({"title": "ERLOTINIB"}, [citation], {citation.source_id: source}, target="EGFR") == []
    altered = BindingEvidence(source_id=citation.source_id, quote=quote.replace("specific", "selective"))
    assert verify_binding({"title": "ERLOTINIB"}, [altered], {citation.source_id: source}, target="EGFR")


@pytest.mark.parametrize("prefix", ["It is not true that ", "It is unknown whether ", "It may be that "])
def test_target_full_name_does_not_remove_context_uncertainty(prefix):
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = "Erlotinib is a highly specific and reversible epidermal growth factor receptor inhibitor"
    assert not mechanism_supported({"title": "ERLOTINIB"}, quote,
                                   {"abstract": prefix + quote + "."}, target="EGFR")


@pytest.mark.parametrize("quote", [
    "Erlotinib is compared with lapatinib, a highly specific and reversible epidermal growth factor receptor inhibitor.",
    "Erlotinib is oral and lapatinib is highly specific and reversible epidermal growth factor receptor inhibitor.",
    "Erlotinib is a highly specific and irreversible epidermal growth factor receptor inhibitor.",
    "Erlotinib is a highly specific and reversible epidermal growth factor receptor2 inhibitor.",
])
def test_target_name_and_adjectives_do_not_transfer_mechanisms(quote):
    from covscreen.ligand_parent_evidence import mechanism_supported
    assert not mechanism_supported({"title": "ERLOTINIB"}, quote, {"abstract": quote}, target="EGFR")


@pytest.mark.parametrize("parent", ["GEFITINIB", "ERLOTINIB"])
def test_coordinated_inhibitor_noun_preserves_citation_target_and_mode(parent):
    quote = "Gefitinib and erlotinib were the first two reversible inhibitors of the EGFR kinase."
    source = {"kind": "literature", "abstract": quote}
    citation = BindingEvidence(source_id="PMID:31857948", quote=quote)
    assert verify_binding({"title": parent}, [citation], {citation.source_id: source}, target="EGFR") == []
    assert verify_binding({"title": parent}, [citation], {citation.source_id: source}, target="HER2")
    assert not mechanism_supported({"title": parent}, quote, source, covalent=True, target="EGFR")
    assert not mechanism_supported({"title": "LAPATINIB"}, quote, source, target="EGFR")
    for prefix in ("It is unknown whether ", "It is incorrect that "):
        assert not mechanism_supported({"title": parent}, quote, {"abstract": prefix + quote}, target="EGFR")
    altered = BindingEvidence(source_id=citation.source_id, quote=quote.replace("first two", "only two"))
    assert verify_binding({"title": parent}, [altered], {citation.source_id: source}, target="EGFR")


@pytest.mark.parametrize("quote", [
    "Gefitinib is inactive and erlotinib is a reversible inhibitor of the EGFR kinase.",
    "Gefitinib and erlotinib were compared with reversible inhibitors of the EGFR kinase.",
    "Gefitinib and erlotinib were not reversible inhibitors of the EGFR kinase.",
    "Gefitinib and erlotinib were irreversible inhibitors of the EGFR kinase.",
])
def test_coordinated_noun_does_not_transfer_comparator_or_negation(quote):
    assert not mechanism_supported({"title": "GEFITINIB"}, quote, {"abstract": quote}, target="EGFR")


def test_retrieved_gefitinib_adjective_list_preserves_exact_citation():
    quote = ("Gefitinib is an orally active, highly selective, reversible inhibitor of the "
             "tyrosine kinase domain associated with the epidermal growth factor receptor (EGFR).")
    source = {"kind": "literature", "abstract": quote}
    citation = BindingEvidence(source_id="PMID:21119248", quote=quote)
    assert verify_binding({"title": "GEFITINIB"}, [citation], {citation.source_id: source}, target="EGFR") == []
    assert verify_binding({"title": "GEFITINIB"}, [citation], {citation.source_id: source}, target="HER2")
    altered = BindingEvidence(source_id=citation.source_id, quote=quote.replace("highly selective", "potent"))
    assert verify_binding({"title": "GEFITINIB"}, [altered], {citation.source_id: source}, target="EGFR")


def test_adjective_list_comparison_retains_independent_geometry_gate():
    evidence, proposal = dataset()
    quote = "ParentBeta is an orally active, highly selective, reversible TEST kinase inhibitor."
    evidence["supplemental_sources"]["PMID:2"]["abstract"] = quote
    proposal["candidates"][1]["binding_evidence"][0]["quote"] = quote
    result = validate_comparison(evidence, proposal, "ParentAlpha")
    assert result["state"] == "PARENT_COMPARISON_REVIEW_READY"
    assert result["execution_ready"] is False
    evidence["parent_geometry"]["geometry-2"]["chemical_identity"] = None
    assert validate_comparison(evidence, proposal, "ParentAlpha")["state"] == "NEEDS_PARENT_EVIDENCE"


@pytest.mark.parametrize("prefix", ["It is not true that ", "It is unknown whether ", "It may be that "])
def test_comma_adjectives_keep_enclosing_context_checks(prefix):
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = "ParentAlpha is an orally active, highly selective, reversible TEST inhibitor"
    assert not mechanism_supported({"title": "ParentAlpha"}, quote,
                                   {"abstract": prefix + quote + "."}, target="TEST")


@pytest.mark.parametrize("quote", [
    "ParentAlpha is orally active, ParentBeta is a reversible TEST inhibitor.",
    "ParentAlpha is orally active, compared with the reversible TEST inhibitor ParentBeta.",
    "ParentAlpha is orally active, selective for OTHER, and ParentBeta is a reversible TEST inhibitor.",
    "ParentAlpha is orally active, highly selective, irreversible TEST inhibitor.",
    "ParentAlpha is orally active, highly selective, reversible OTHER inhibitor.",
])
def test_comma_adjectives_cannot_transfer_subject_target_or_mode(quote):
    from covscreen.ligand_parent_evidence import mechanism_supported
    assert not mechanism_supported({"title": "ParentAlpha"}, quote, {"abstract": quote}, target="TEST")


def test_accession_and_gene_inputs_have_identical_mechanism_decisions():
    from covscreen.ligand_parent_evidence import parent_mechanism_query, verified_target_gene
    evidence, proposal = dataset()
    expected = validate_comparison(evidence, proposal, 'ParentAlpha')
    evidence['target'] = 'TEST01'
    evidence['identity'].update(gene='TEST', requested_target='TEST01', taxon=9606)
    assert verified_target_gene(evidence) == 'TEST'
    assert validate_comparison(evidence, proposal, 'ParentAlpha') == expected
    query = parent_mechanism_query(evidence, 'ChEMBL:CHEMBL1')
    assert '"TEST"' in query and 'TEST01' not in query
    assert evidence['target'] == 'TEST01'


@pytest.mark.parametrize('field,value', [('requested_target', 'OTHER'), ('taxon', 10090),
    ('uniprot_id', 'OTHER1'), ('canonical_cys_verified', False), ('gene', None)])
def test_accession_gene_resolution_rejects_unbound_identity(field, value):
    from covscreen.ligand_parent_evidence import verified_target_gene
    evidence, _ = dataset()
    evidence['target'] = 'TEST01'
    evidence['identity'].update(gene='TEST', requested_target='TEST01', taxon=9606)
    evidence['identity'][field] = value
    with pytest.raises(ValueError):
        verified_target_gene(evidence)


@pytest.mark.parametrize('parent', ['Erlotinib', 'gefitinib'])
def test_coordinated_parents_with_explicit_reversible_inhibition(parent):
    quote = ('Erlotinib and gefitinib are quinazoline derivatives that selectively '
             'and reversibly inhibit the tyrosine kinase activity of the EGFR.')
    source = {'kind': 'literature', 'abstract': quote}
    citation = BindingEvidence(source_id='PMID:21677478', quote=quote)
    assert verify_binding({'title': parent}, [citation], {'PMID:21677478': source}, target='EGFR') == []


@pytest.mark.parametrize('quote', [
    'ParentAlpha and ParentBeta may reversibly inhibit the tyrosine kinase activity of TEST.',
    'ParentAlpha and ParentBeta do not reversibly inhibit the tyrosine kinase activity of TEST.',
    'ParentAlpha and ParentBeta reversibly inhibit the tyrosine kinase activity of OTHER, compared with TEST.',
    'ParentAlpha was compared with ParentBeta which reversibly inhibits TEST.',
    'ParentAlpha activates TEST and ParentBeta reversibly inhibits TEST.',
    'ParentAlpha and ParentBeta irreversibly inhibit the tyrosine kinase activity of TEST.',
])
def test_adverb_mechanism_cannot_transfer_subject_target_or_negation(quote):
    from covscreen.ligand_parent_evidence import mechanism_supported
    assert not mechanism_supported({'title': 'ParentAlpha'}, quote, {'abstract': quote}, target='TEST')


def test_initial_literature_and_supplemented_literature_have_same_gate():
    evidence, proposal = dataset()
    expected = validate_comparison(evidence, proposal, 'ParentAlpha')
    for key in ['PMID:1', 'PMID:2']:
        record = evidence['supplemental_sources'].pop(key)
        record.pop('kind')
        evidence['literature'].append({**record, 'id': key, 'source': 'PubMed'})
    original = copy.deepcopy(evidence)
    assert validate_comparison(evidence, proposal, 'ParentAlpha') == expected
    assert evidence == original
    proposal['candidates'][0]['binding_evidence'][0]['quote'] += ' Invented.'
    assert validate_comparison(evidence, proposal, 'ParentAlpha')['state'] == 'NEEDS_PARENT_EVIDENCE'


@pytest.mark.parametrize('parent,target,covalent,expected', [
    ('Afatinib', 'EGFR', True, True), ('Afatinib', 'HER2', True, True),
    ('Afatinib', 'HER3', True, False), ('Erlotinib', 'EGFR', True, False),
    ('Afatinib', 'EGFR', False, False),
])
def test_explicit_covalently_binding_target_list(parent, target, covalent, expected):
    from covscreen.ligand_parent_evidence import mechanism_supported
    quote = 'Afatinib is a small molecule covalently binding and inhibiting the EGFR, HER2 and HER4 receptor tyrosine kinases.'
    assert mechanism_supported({'title': parent}, quote, {'abstract': quote},
                               target=target, covalent=covalent) is expected
    assert not mechanism_supported({'title': parent}, quote,
                                   {'abstract': 'It is not true that ' + quote},
                                   target=target, covalent=covalent)
