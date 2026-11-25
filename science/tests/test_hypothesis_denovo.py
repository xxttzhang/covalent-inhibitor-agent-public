import json
import shutil
from pathlib import Path

import pytest

from covscreen.hypothesis_denovo import handoff
from covscreen.target_hypothesis import finalize
from test_target_hypothesis import evidence, proposal as base_proposal

ROOT = Path(__file__).resolve().parents[1]


def proposal(route="de-novo"):
    value = base_proposal(route)
    value["warhead_selection"] = {
        "selected_warhead": "acrylamide", "statement": "Synthetic test-only site suitability rationale",
        "evidence_ids": ["PMID:test"], "interpretation": "inference",
        "alternatives": [{"warhead": "propiolamide", "disposition": "backup",
            "statement": "Synthetic test-only alternative rationale",
            "evidence_ids": ["PMID:test"], "interpretation": "inference"}]}
    return value


def setup(root):
    files = ["target_passport.json", "assessment/covalent_assessment.json",
        "plans/route_plan.json", "plans/de_novo_request.example.json"]
    for name in files:
        target = root / "projects/egfr-c797" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "projects/egfr-c797" / name, target)
    (root / "configs").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "configs/local.inspire.yaml", root / "configs/local.inspire.yaml")
    return {"route": "de-novo", "target": "EGFR", "site": "C797", "taxon": 9606,
        "uniprot_id": "P00533", "request_path": "projects/egfr-c797/plans/de_novo_request.example.json"}


def test_unregistered_denovo_is_explicitly_unsupported(evidence):
    root, identifier = evidence
    result = finalize(root, identifier, proposal("de-novo"))
    assert result["handoff"]["state"] == "UNSUPPORTED"
    assert result["handoff"]["next_call"] is None
    assert result["handoff"]["compute_submitted"] is False


def test_native_study_delivers_to_host_without_legacy_template_or_execution(evidence):
    root, identifier = evidence
    value = proposal()
    value['study_id'] = 'study-00000000-0000-4000-8000-000000000001'
    result = finalize(root, identifier, value)
    handoff = result['handoff']
    assert handoff['state'] == 'HOST_PLAN_REQUIRED'
    assert handoff['next_call']['arguments'] == {'action': 'sync-hypotheses', 'study_id': value['study_id']}
    assert handoff['compiled'] is False
    assert handoff['approved'] is False
    assert handoff['compute_submitted'] is False
    assert Path(result['artifacts']['report']).is_file()
    assert not (root / 'state/jobs').exists()
    assert not (root / 'projects').exists()


def test_real_denovo_compiler_receives_fresh_request_without_approvals(evidence):
    root, identifier = evidence
    profile = setup(root)
    (root / "configs/target_hypothesis_routes.json").write_text(json.dumps({"profiles": [profile]}))
    hypothesis = proposal("de-novo")
    source = root / profile["request_path"]
    request = json.loads(source.read_text())
    request["allowed_warheads"] = ["acrylamide"]
    source.write_text(json.dumps(request))
    first = finalize(root, identifier, hypothesis)["handoff"]
    second = finalize(root, identifier, hypothesis)["handoff"]
    assert first["state"] == "PLAN_COMPILED"
    assert first["request"]["request_id"] != second["request"]["request_id"]
    assert first["route_plan"]["plan"]["decision"] == "BLOCKED"  # no inherited route override
    for flag in ("human_approved_generation", "human_approved_covalent_screen", "route_override_approved"):
        assert first["request"][flag] is False
    assert first["historical_candidates_reused"] is False
    assert first["compute_submitted"] is False
    assert not (root / "state/jobs").exists()


def test_changed_pocket_and_target_are_not_replaced_with_default(evidence):
    root, identifier = evidence
    profile = setup(root)
    request_path = root / profile["request_path"]
    request = json.loads(request_path.read_text())
    request["pocket"]["center_xyz_angstrom"] = [11.5, -2.0, 9.0]
    request["pocket"]["structure_reference"] = "test-only:other-structure"
    request_path.write_text(json.dumps(request))
    ev = json.loads((root / "results/target-hypotheses" / identifier / "evidence.json").read_text())
    result = handoff(root, ev, request["allowed_warheads"], profile)
    assert result["request"]["pocket"] == request["pocket"]
    assert json.loads((root / result["request_path"]).read_text())["pocket"]["center_xyz_angstrom"] == [11.5, -2.0, 9.0]
    ev.update(target="JAK3", site="C909")
    ev["identity"]["uniprot_id"] = "P52333"
    with pytest.raises(ValueError, match="does not match"):
        handoff(root, ev, request["allowed_warheads"], profile)


def test_historical_candidates_cannot_enter_a_new_request(evidence):
    root, identifier = evidence
    profile = setup(root)
    source = root / profile["request_path"]
    request = json.loads(source.read_text())
    request["scaffold_candidates_path"] = "old-candidates.sdf"
    source.write_text(json.dumps(request))
    ev = json.loads((root / "results/target-hypotheses" / identifier / "evidence.json").read_text())
    with pytest.raises(ValueError, match="cannot inherit"):
        handoff(root, ev, request["allowed_warheads"], profile)


@pytest.mark.parametrize('selection', [None, {}])
def test_denovo_requires_warhead_decision(evidence, selection):
    root, identifier = evidence
    value = base_proposal('de-novo')
    if selection is not None:
        value['warhead_selection'] = selection
    with pytest.raises(ValueError):
        finalize(root, identifier, value)
    assert not list((root / 'results/target-hypotheses' / identifier).glob('proposal-*'))


@pytest.mark.parametrize('where', ['primary', 'alternative'])
def test_warhead_citations_are_validated_before_handoff(evidence, where):
    root, identifier = evidence
    value = proposal()
    selection = value['warhead_selection']
    claim = selection if where == 'primary' else selection['alternatives'][0]
    claim['evidence_ids'] = ['PMID:invented']
    with pytest.raises(ValueError, match='Unknown evidence IDs'):
        finalize(root, identifier, value)
    assert not list((root / 'results/target-hypotheses' / identifier).glob('proposal-*'))


@pytest.mark.parametrize('warheads', [['propiolamide'], ['acrylamide', 'propiolamide']])
def test_execution_scope_cannot_replace_or_expand_selection(evidence, warheads):
    root, identifier = evidence
    value = proposal()
    value['warhead_class'] = warheads
    with pytest.raises(ValueError, match='only the selected warhead'):
        finalize(root, identifier, value)


@pytest.mark.parametrize('mutation', ['empty_alternatives', 'duplicate', 'no_evidence', 'no_reason'])
def test_incomplete_or_duplicate_comparisons_fail(evidence, mutation):
    root, identifier = evidence
    value = proposal()
    selection = value['warhead_selection']
    if mutation == 'empty_alternatives':
        selection['alternatives'] = []
    elif mutation == 'duplicate':
        selection['alternatives'][0]['warhead'] = ' ACRYLAMIDE '
    elif mutation == 'no_evidence':
        selection['alternatives'][0]['evidence_ids'] = []
    else:
        selection['statement'] = ''
    with pytest.raises(ValueError):
        finalize(root, identifier, value)


def test_decision_and_all_references_are_delivered(evidence):
    from covscreen.target_hypothesis import digest
    root, identifier = evidence
    directory = root / 'results/target-hypotheses' / identifier
    path = directory / 'evidence.json'
    ev = json.loads(path.read_text())
    ev['literature'].append({'id': 'PMID:alternative', 'title': 'Synthetic alternative evidence'})
    path.write_text(json.dumps(ev))
    (directory / 'manifest.json').write_text(json.dumps({'files': {'evidence.json': digest(path)}}))
    value = proposal()
    value['warhead_selection']['alternatives'][0]['evidence_ids'] = ['PMID:alternative']
    result = finalize(root, identifier, value)
    saved = json.loads(Path(result['artifacts']['hypothesis']).read_text())
    from covscreen.target_hypothesis import WarheadSelection
    assert saved['hypothesis']['warhead_selection'] == WarheadSelection.model_validate(value['warhead_selection']).model_dump()
    assert {item['id'] for item in saved['references']} == {'PMID:test', 'PMID:alternative'}
    report = Path(result['artifacts']['report']).read_text()
    assert '首选：acrylamide' in report
    assert 'propiolamide（backup）' in report
    assert 'PMID:alternative' in report
    assert result['handoff']['compute_submitted'] is False


def test_mcp_schema_exposes_conditional_selection():
    import jsonschema
    from covscreen.mcp.upstream import TOOLS
    schema = TOOLS[0]['inputSchema']
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate({'action': 'finalize', 'hypothesis': proposal()})
    for value in [base_proposal('de-novo'), {**base_proposal('de-novo'), 'warhead_selection': None}]:
        assert list(validator.iter_errors({'action': 'finalize', 'hypothesis': value}))
    for route in ['library-first', 'ligand-first']:
        validator.validate({'action': 'finalize', 'hypothesis': base_proposal(route)})


def test_unresolved_decision_is_saved_without_compiling(evidence):
    root, identifier = evidence
    value = proposal()
    value['warhead_class'] = []
    value['warhead_selection'] = {'status': 'unresolved', 'selected_warhead': None,
        'execution_branch_id': None, 'statement': 'Insufficient test evidence',
        'interpretation': 'inference', 'evidence_ids': [], 'pending_reason': 'Need site-specific evidence',
        'no_alternatives_reason': 'No evidence-backed alternatives available'}
    result = finalize(root, identifier, value)
    assert result['handoff']['state'] == 'NEEDS_SCIENTIFIC_DECISION'
    assert result['handoff']['compute_submitted'] is False
    assert result['handoff']['next_call'] is None
    assert Path(result['artifacts']['hypothesis']).is_file()


def test_no_alternatives_with_reason_is_valid(evidence):
    root, identifier = evidence
    value = proposal()
    value['warhead_selection']['alternatives'] = []
    value['warhead_selection']['no_alternatives_reason'] = 'Limited evidence coverage in fixture'
    result = finalize(root, identifier, value)
    assert result['hypothesis']['warhead_selection']['alternatives'] == []


def test_independent_branch_scope_is_explicit(evidence):
    from covscreen.target_hypothesis import Hypothesis
    root, identifier = evidence
    value = proposal()
    value['warhead_selection']['branches'] = [{'branch_id': 'propiolamide-pilot',
        'warhead': 'propiolamide', 'statement': 'Test-only separate branch rationale',
        'evidence_ids': ['PMID:test'], 'interpretation': 'inference'}]
    assert Hypothesis.model_validate(value).warhead_class == ['acrylamide']
    value['warhead_selection']['execution_branch_id'] = 'propiolamide-pilot'
    with pytest.raises(ValueError, match='execution branch'):
        Hypothesis.model_validate(value)
    value['warhead_class'] = ['propiolamide']
    result = finalize(root, identifier, value)
    assert result['handoff']['execution_branch_id'] == 'propiolamide-pilot'
    assert result['handoff']['compute_submitted'] is False
    assert result['hypothesis']['warhead_selection']['selected_warhead'] == 'acrylamide'
