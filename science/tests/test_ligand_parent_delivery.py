import base64
import hashlib
import json

import pytest

from covscreen.ligand_parent_delivery import LIMIT, export_parent_report


@pytest.fixture
def completed_design_evidence():
    identity = {'workflow_id': 'ligand-flow-' + 'a' * 24,
                'design_id': 'design-' + 'b' * 24, 'evidence_id': 'hyp-' + 'c' * 24}
    source = {**identity, 'generated_candidate_count': 4,
              'selected_candidate_ids': ['first', 'second'],
              'candidate_deferrals': [
                  {'candidate_id': 'third', 'reason': 'UNRESOLVED_STEREOCHEMISTRY', 'centers': [23]},
                  {'candidate_id': 'fourth', 'reason': 'UNRESOLVED_STEREOCHEMISTRY', 'centers': [23]}]}
    report = {**identity, 'execution': {'state': 'COMPLETED', 'return_code': 0},
              'evaluation': {'run_id': 'existing-lo', 'gate_algorithm_version': 'test-policy',
                             'candidate_count': 2, 'candidates': [
                  {'compound_id': 'first', 'engine_status': 'SUCCESS', 'failure_reason': None,
                   'gate': {'review_eligible': True, 'covdock_gate_reason': 'Checks passed'}},
                  {'compound_id': 'second', 'engine_status': 'FAILED', 'failure_reason': 'No output structure',
                   'gate': {'review_eligible': False, 'covdock_gate_reason': 'Evidence missing'}}]}}
    return source, report


def test_completed_design_keeps_failed_and_deferred_denominators(completed_design_evidence):
    from covscreen.ligand_design_delivery import completed_design
    source, report = completed_design_evidence
    before = json.dumps([source, report], sort_keys=True)
    result, markdown = completed_design(source, report)
    assert result['counts'] == {'generated': 4, 'deferred': 2, 'docked': 2, 'passed': 1}
    assert 'No output structure' in markdown
    assert 'UNRESOLVED_STEREOCHEMISTRY' in markdown
    assert result['candidate_deferrals'][0]['centers'] == [23]
    assert result['md_ready'] is False
    assert result['compute_submitted_by_report'] is False
    assert json.dumps([source, report], sort_keys=True) == before


@pytest.mark.parametrize('change', ['foreign', 'duplicate', 'missing', 'overlap', 'wrong_total'])
def test_completed_design_refuses_inconsistent_evidence(completed_design_evidence, change):
    from covscreen.ligand_design_delivery import completed_design
    source, report = completed_design_evidence
    if change == 'foreign':
        report['design_id'] = 'design-' + 'd' * 24
    elif change == 'duplicate':
        report['evaluation']['candidates'][1]['compound_id'] = 'first'
    elif change == 'missing':
        report['evaluation']['candidates'].pop()
    elif change == 'overlap':
        source['candidate_deferrals'][0]['candidate_id'] = 'first'
    else:
        source['generated_candidate_count'] = 3
    with pytest.raises(ValueError):
        completed_design(source, report)


@pytest.fixture
def delivery(tmp_path):
    evidence = "hyp-" + "a" * 24
    proposal = "proposal-" + "b" * 12
    folder = tmp_path / "results/target-hypotheses" / evidence / proposal
    folder.mkdir(parents=True)
    raw = json.dumps({"evidence_id": evidence, "hypothesis": {"route": "ligand-first"}}).encode()
    (folder / "hypothesis.json").write_bytes(raw)
    (folder / "report.md").write_bytes("# 原始报告\n未开展计算。\n".encode())
    state = {"workflow_id": "ligand-flow-" + "c" * 24, "evidence_id": evidence,
             "proposal": {"evidence_id": evidence, "proposal_id": proposal,
                          "proposal_sha256": hashlib.sha256(raw).hexdigest()}}
    return tmp_path, folder, state


def test_exports_exact_bytes_without_mutating_state(delivery):
    root, folder, state = delivery
    before = json.dumps(state, sort_keys=True)
    result = export_parent_report(root, state)
    assert result["compute_submitted"] is False
    assert json.dumps(state, sort_keys=True) == before
    for item in result["files"]:
        raw = base64.b64decode(item["base64"], validate=True)
        assert raw == (folder / item["name"]).read_bytes()
        assert len(raw) == item["bytes"]
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]


def test_pair_deferral_is_exported_without_rewriting_original_report(delivery):
    root, folder, state = delivery
    original = (folder / 'report.md').read_bytes()
    state.update(revision=4, parent_pair_review={'status': 'EVIDENCE_INSUFFICIENT',
        'reasons': ['cys_connection_requires_review'], 'execution_ready': False})
    result = export_parent_report(root, state)
    item = next(row for row in result['files'] if row['name'] == 'report.md')
    raw = base64.b64decode(item['base64'])
    assert raw.startswith(original)
    assert b'cys_connection_requires_review' in raw
    assert hashlib.sha256(raw).hexdigest() == item['sha256']
    assert (folder / 'report.md').read_bytes() == original


@pytest.mark.parametrize("change", ["hash", "other_evidence", "traversal", "missing_binding"])
def test_rejects_invalid_current_binding(delivery, change):
    root, folder, state = delivery
    if change == "hash":
        (folder / "hypothesis.json").write_text("{}")
    elif change == "other_evidence":
        state["evidence_id"] = "hyp-" + "d" * 24
    elif change == "traversal":
        state["proposal"]["proposal_id"] = "../outside"
    else:
        state.pop("proposal")
    with pytest.raises(ValueError):
        export_parent_report(root, state)


def test_rejects_report_symlink(delivery):
    root, folder, state = delivery
    (root / "outside.md").write_text("not a proposal")
    (folder / "report.md").unlink()
    (folder / "report.md").symlink_to(root / "outside.md")
    with pytest.raises(ValueError, match="symlinks"):
        export_parent_report(root, state)


def test_rejects_oversized_report(delivery):
    root, folder, state = delivery
    (folder / "report.md").write_bytes(b"x" * (LIMIT + 1))
    with pytest.raises(ValueError, match="limit"):
        export_parent_report(root, state)


@pytest.fixture
def design_delivery(tmp_path):
    from covscreen.ligand_design import seal
    design = 'design-' + 'd' * 24
    evidence = 'hyp-' + 'e' * 24
    folder = tmp_path / 'results/ligand-designs' / design
    folder.mkdir(parents=True)
    (folder / 'design.json').write_text(json.dumps({'design_id': design, 'evidence_id': evidence}))
    seal(folder)
    generation = folder / 'generation'
    generation.mkdir()
    (generation / 'summary.json').write_text('{"candidate_count": 0}')
    seal(generation)
    delivery = folder / 'delivery'
    delivery.mkdir()
    (delivery / 'disposition.json').write_text(json.dumps({'design_id': design, 'evidence_id': evidence,
        'binding': {'design_manifest_sha256': hashlib.sha256((folder / 'manifest.json').read_bytes()).hexdigest(),
                    'generation_manifest_sha256': hashlib.sha256((generation / 'manifest.json').read_bytes()).hexdigest()}}))
    (delivery / 'report.md').write_text('# 原件\n零候选不代表靶点不可行。\n')
    seal(delivery)
    return tmp_path, folder, {'workflow_id': 'ligand-flow-' + 'f' * 24, 'design_id': design, 'branches': []}


def test_design_current_and_archived_exact_export(design_delivery):
    from covscreen.ligand_design_delivery import export_design_report
    root, folder, state = design_delivery
    current = export_design_report(root, state)
    for file in current['files']:
        assert base64.b64decode(file['base64']) == (folder / 'delivery' / file['name']).read_bytes()
    selected = state.pop('design_id')
    state['branches'] = [{'design_id': selected}]
    assert export_design_report(root, state, selected) == current
    with pytest.raises(ValueError, match='belong'):
        export_design_report(root, state, 'design-' + 'a' * 24)


@pytest.mark.parametrize('change', ['report', 'disposition', 'generation', 'design', 'symlink', 'oversize'])
def test_design_export_refuses_tampering(design_delivery, change):
    from covscreen.ligand_design_delivery import export_design_report
    root, folder, state = design_delivery
    files = {'report': 'delivery/report.md', 'disposition': 'delivery/disposition.json',
             'generation': 'generation/summary.json', 'design': 'design.json'}
    if change in files:
        (folder / files[change]).write_text('{}')
    elif change == 'symlink':
        target = folder / 'delivery/report.md'
        target.unlink()
        target.symlink_to(folder / 'design.json')
    else:
        (folder / 'delivery/report.md').write_bytes(b'x' * (LIMIT + 1))
    with pytest.raises(ValueError):
        export_design_report(root, state)


def test_design_workflow_read_action_schema_and_revision(design_delivery):
    from covscreen.ligand_workflow import execute_step, EVIDENCE, READS, FIELDS
    from covscreen.mcp.ligand_workflow import TOOLS
    root, folder, state = design_delivery
    before = json.dumps(state, sort_keys=True)
    result = execute_step(root, state, EVIDENCE, 'export_design_report', {})
    assert result['design_id'] == state['design_id']
    assert 'export_design_report' in READS
    assert FIELDS['export_design_report'] == ['design_id']
    assert json.dumps(state, sort_keys=True) == before
    schema = next(t['inputSchema'] for t in TOOLS if t['name'] == EVIDENCE)
    assert 'export_design_report' in schema['properties']['action']['enum']
    assert 'design_id' in schema['properties']
