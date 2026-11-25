"""Readback policy and ledger checks over explicitly artificial scientific data."""
import copy
import json

import pytest

from covscreen import covdock_readback, refinement_output
from covscreen.ligand_covdock_evidence import approved_policy, evaluate_outputs, witness
from covscreen.ligand_covdock_report import freeze
from covscreen.ligand_covdock_execution import reject_evidence
from test_refinement_job_contract import configured
from covdock_readback_fixture import outputs


@pytest.fixture
def case(tmp_path, monkeypatch):
    manager, request, _ = configured(tmp_path, 'schrodinger-covdock-lo')
    request.parameters.update(nucleophilic_atom='SG', prepare_receptor=True, resume=False,
                              environment_review={'artifact_id': 'artificial-environment-review'})
    spec = manager.build_spec(request)
    spec.parameters['covdock_readback'] = covdock_readback.freeze(manager.config['schrodinger'], 'A:797')
    manager.registry.write_spec(spec)
    state = {'workflow_id': 'artificial-flow', 'design_id': 'fixture', 'evidence_id': 'artificial-evidence',
        'target': {'target': 'EGFR', 'site': 'C797'}, 'refinement_input': {'artifact_id': 'artificial-input'},
        'covdock_execution': {'run_id': spec.run_id, 'job_state': 'FAILED', 'return_code': 2}}
    def build(**kwargs):
        result, sdk = outputs(tmp_path, manager, spec, **kwargs)
        monkeypatch.setattr(refinement_output, '_covdock_sdk', sdk)
        return result, sdk
    return tmp_path, manager, spec, state, build


def evaluate(case, result, **kwargs):
    root, manager, spec, state, _ = case
    return evaluate_outputs(root, state, manager, spec, result, **kwargs)


def retain(case, review):
    root, _, _, state, _ = case
    state['covdock_execution'].update(evaluation=review, evaluation_artifact=witness(root, review),
        evaluation_verification='verified')


def test_registered_quality_exit_four_can_be_reviewed_without_acceptance(case):
    result, _ = case[4](missing_strain=True)
    result.return_code = 4
    review = evaluate(case, result)
    assert review['candidate_count'] == 2
    assert not review['md_ready'] and not review['scientific_result_verified']
    assert review['candidates'][0]['gate']['physical_validity'] == 'UNKNOWN'
    assert not review['candidates'][0]['gate']['review_eligible']
    # Exit 4 is not a bypass for changed registered scientific output.
    (case[1].registry.job_dir(case[2].run_id) / 'artifacts/refinement_artifacts.json').write_text('{}')
    with pytest.raises(ValueError):
        evaluate(case, result)


@pytest.mark.parametrize('job_state,code,allowed', [
    ('COMPLETED', 0, True), ('FAILED', 2, True), ('FAILED', 4, True),
    ('FAILED', 1, False), ('RUNNING', None, False), ('TIMED_OUT', 4, False)])
def test_web_workflow_exposes_same_readback_outcomes(job_state, code, allowed):
    from covscreen.ligand_workflow import view, DESIGN
    state = {'workflow_id': 'artificial-flow', 'revision': 1, 'stage': 'COVDOCK_EXECUTION_REVIEW',
        'branches': [], 'design_id': 'fixture', 'evidence_id': 'fixture',
        'covdock_execution': {'job_state': job_state, 'return_code': code}}
    actions = view(state)['tools'][DESIGN]['actions']
    assert ('evaluate_covdock' in actions) is allowed
    assert ('report_covdock' in actions) is allowed
    assert 'start_covdock' not in actions
    assert 'prepare_md_handoff' not in actions
    state['covdock_execution']['evidence_error'] = 'registered output changed'
    assert 'evaluate_covdock' not in view(state)['tools'][DESIGN]['actions']


def test_old_af3_missing_batch_is_reassessed_without_overwriting_producer(case):
    from covscreen.covdock_gate import LIGAND_ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION
    from covscreen.jobs import sha256_file
    result, _ = case[4](partial=False)
    result.state, result.return_code = 'FAILED', 4
    folder = case[1].registry.job_dir(case[2].run_id) / 'artifacts'
    before = {str(p): sha256_file(p) for p in folder.rglob('*') if p.is_file()}
    review = evaluate(case, result)
    assert review['policy_reassessment'] is True
    assert review['gate_algorithm_version'] == LIGAND_ALGORITHM_VERSION
    assert review['original_gate_algorithm_version'] == STRICT_ALGORITHM_VERSION
    for candidate in review['candidates']:
        assert candidate['original_gate']['covdock_gate_detail'] == 'B2'
        assert not candidate['original_gate']['review_eligible']
        assert candidate['gate']['review_eligible']
        assert candidate['gate']['cross_method_consensus'] == 'NA'
    assert before == {str(p): sha256_file(p) for p in folder.rglob('*') if p.is_file()}
    assert not review['md_ready'] and not review['scientific_result_verified']
    retain(case, review)
    report = freeze(case[0], case[3])
    text = (case[0] / report['report_markdown']).read_text()
    assert 'AF3 比较为可选' in text and '原冻结分组：B2' in text


def test_different_reader_versions_preserve_provenance_and_exact_measurements(case):
    from covscreen.ligand_covdock_evidence import same_receptor_measurements
    result, _ = case[4](partial=False, producer_reader_version='2022.09.4')
    review = evaluate(case, result)
    versions = review['receptor_reader_versions']
    assert versions['producer_input'] == versions['producer_effective'] == '2022.09.4'
    assert versions['approved_input'] == case[2].parameters['covdock_receptor_input_check']['reader_version']
    original = review['effective_receptor_check']
    changed = copy.deepcopy(original)
    changed['reader_version'] = '2022.09.4'
    assert same_receptor_measurements(original, changed)
    changed['target']['xyz'][0] += 0.001
    assert not same_receptor_measurements(original, changed)
    changed = {**original, 'receptor_sha256': '0' * 64}
    assert not same_receptor_measurements(original, changed)
    assert not same_receptor_measurements(original, {**original, 'reader': 'another-reader'})


def residue_check(rows):
    import hashlib
    from covscreen.refinement_output import canonical
    return {'observed_residues': rows, 'observed_residue_count': len(rows),
            'observed_residue_identity_sha256': hashlib.sha256(canonical(rows)).hexdigest()}


@pytest.mark.parametrize('name', ['HID', 'HIE', 'HIP'])
def test_prepared_histidine_microstates_are_retained_not_mutations(name):
    from covscreen.ligand_covdock_evidence import compare_prepared_residues
    row = {'chain': 'A', 'residue_number': 781, 'insertion_code': '', 'residue_name': 'HIS'}
    before = residue_check([row])
    after = residue_check([{**row, 'residue_name': name}])
    result = compare_prepared_residues(before, after)
    assert result['observed_identity_preserved']
    assert result['histidine_microstate_changes'][0]['prepared_name'] == name
    assert not result['protonation_suitability_verified']
    assert before['observed_residues'][0]['residue_name'] == 'HIS'


@pytest.mark.parametrize('change', [
    {'residue_name': 'ALA'}, {'residue_name': 'HIC'}, {'chain': 'B'},
    {'residue_number': 782}, {'insertion_code': 'A'}])
def test_preparation_cannot_mask_mutation_or_remapping(change):
    from covscreen.ligand_covdock_evidence import compare_prepared_residues
    row = {'chain': 'A', 'residue_number': 781, 'insertion_code': '', 'residue_name': 'HIS'}
    with pytest.raises(ValueError):
        compare_prepared_residues(residue_check([row]), residue_check([{**row, **change}]))


def test_preparation_cannot_mask_missing_extra_or_unbound_inventory():
    from covscreen.ligand_covdock_evidence import compare_prepared_residues
    row = {'chain': 'A', 'residue_number': 781, 'insertion_code': '', 'residue_name': 'HIS'}
    before = residue_check([row])
    for rows in ([], [row, {**row, 'residue_number': 782}], [row, row]):
        with pytest.raises(ValueError):
            compare_prepared_residues(before, residue_check(rows))
    after = copy.deepcopy(before)
    after['observed_residues'][0]['residue_name'] = 'HIP'
    with pytest.raises(ValueError, match='differs from its inspection'):
        compare_prepared_residues(before, after)


def test_report_exposes_preparation_microstate_changes(case):
    result, _ = case[4](partial=False)
    review = evaluate(case, result)
    assert review['prepared_residue_identity']['observed_identity_preserved']
    review['prepared_residue_identity']['histidine_microstate_changes'] = [
        {'chain': 'A', 'residue_number': 781, 'insertion_code': '',
         'input_name': 'HIS', 'prepared_name': 'HIP'}]
    retain(case, review)
    report = freeze(case[0], case[3])
    text = (case[0] / report['report_markdown']).read_text()
    assert 'A:781：HIS → HIP' in text
    assert '不代表其生物学适宜性已获验证' in text


def test_lo_export_delivers_exact_sealed_bytes_and_rejects_changes(case):
    import base64
    from covscreen.ligand_covdock_report import export_report
    from covscreen.ligand_workflow import execute_step, EVIDENCE
    root, _, _, state, build = case
    with pytest.raises(ValueError):
        export_report(root, state)

    result, _ = build(partial=False)
    retain(case, evaluate(case, result))
    reference = freeze(root, state)
    state['covdock_execution']['report'] = reference
    delivery = execute_step(root, state, EVIDENCE, 'export_covdock_report', {})
    assert delivery['schema'] == 'ligand-covdock-delivery-v1'
    assert not delivery['compute_submitted']
    assert delivery == export_report(root, state)
    for item in delivery['files']:
        key = 'report_json' if item['name'] == 'report.json' else 'report_markdown'
        assert base64.b64decode(item['base64']) == (root / reference[key]).read_bytes()
    state['covdock_execution']['evaluation_verification'] = 'evidence_rejected'
    with pytest.raises(ValueError):
        export_report(root, state)
    state['covdock_execution']['evaluation_verification'] = 'verified'
    (root / reference['report_markdown']).write_text('changed report')
    with pytest.raises(ValueError):
        export_report(root, state)


def test_lo_export_is_exposed_only_after_verified_report():
    from covscreen.ligand_workflow import view, EVIDENCE, FIELDS
    from covscreen.mcp.ligand_workflow import schema
    state = {'workflow_id': 'artificial-flow', 'revision': 1, 'stage': 'COVDOCK_EXECUTION_REVIEW',
             'branches': [], 'design_id': 'fixture', 'evidence_id': 'fixture',
             'covdock_execution': {'job_state': 'FAILED', 'return_code': 4}}
    assert 'export_covdock_report' not in view(state)['tools'][EVIDENCE]['actions']
    state['covdock_execution'].update(report={'artifact_id': 'sealed'}, evaluation_verification='verified')
    assert 'export_covdock_report' in view(state)['tools'][EVIDENCE]['actions']
    state['covdock_execution']['evidence_error'] = 'changed source'
    assert 'export_covdock_report' not in view(state)['tools'][EVIDENCE]['actions']
    assert FIELDS['export_covdock_report'] == []
    assert 'export_covdock_report' in schema['properties']['action']['enum']


def test_reassessment_verifies_retained_reader_without_rebinding_original(case):
    from pathlib import Path
    from covscreen.jobs import sha256_file
    root, _, spec, _, build = case
    binding = copy.deepcopy(spec.parameters['covdock_readback'])
    original_binding = copy.deepcopy(binding)
    current_script = Path(binding['script'])
    retained = root / 'retained-release/science'
    script = retained / 'scripts' / current_script.name
    script.parent.mkdir(parents=True)
    script.write_bytes(current_script.read_bytes())
    for module in binding['sources']:
        relative = Path('src') / (module.replace('.', '/') + '.py')
        target = retained / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (current_script.parent.parent / relative).read_bytes()
        if module == 'covscreen.ligand_covdock_evidence':
            raw += b'\n# Artificial historical evaluator revision\n'
        target.write_bytes(raw)
        binding['sources'][module] = sha256_file(target)
    binding['assets'][str(script)] = binding['assets'].pop(str(current_script))
    binding['script'] = str(script)
    frozen = copy.deepcopy(binding)
    spec.parameters['covdock_readback'] = binding
    with pytest.raises(ValueError, match='differs from the frozen plan'):
        covdock_readback.validate(binding, 'A:797')
    result, _ = build(partial=False)
    review = evaluate(case, result)
    assert review['frozen_readback_sources'] == frozen['sources']
    assert review['implementation_sha256']['covscreen.ligand_covdock_evidence'] != frozen['sources']['covscreen.ligand_covdock_evidence']
    assert binding == frozen and binding != original_binding
    with pytest.raises(ValueError):
        covdock_readback.validate(binding, 'A:798', allow_reassessment=True)
    script.write_bytes(script.read_bytes() + b'\n# changed\n')
    with pytest.raises(ValueError):
        covdock_readback.validate(binding, 'A:797', allow_reassessment=True)
    script.write_bytes(current_script.read_bytes())
    target = retained / 'src/covscreen/ligand_covdock_evidence.py'
    target.write_text('changed original source')
    with pytest.raises(ValueError):
        covdock_readback.validate(binding, 'A:797', allow_reassessment=True)


@pytest.mark.parametrize('code', [1, 3, 137])
def test_engine_error_exits_do_not_become_quality_results(case, code):
    result, _ = case[4](missing_strain=True)
    result.return_code = code
    with pytest.raises(ValueError, match='terminal batch'):
        evaluate(case, result)


def test_partial_census_unknown_consensus_cache_and_immutable_reports(case, monkeypatch):
    root, _, _, state, build = case
    result, sdk = build(missing_strain=True)
    review = evaluate(case, result)
    assert review['candidate_count'] == 2 and review['failed_candidate_count'] == 1
    first, failed = review['candidates']
    assert first['compound_id'] == '007'
    assert first['gate']['physical_validity'] == 'UNKNOWN'
    assert first['gate']['cross_method_consensus'] == 'NA' and not first['gate']['review_eligible']
    assert failed['measurements'] is None and failed['assessment'] == 'evidence_insufficient'
    assert not review['md_ready'] and not review['scientific_result_verified']
    retain(case, review)
    report = freeze(root, state)
    state['covdock_execution']['report'] = report
    text = (root / report['report_markdown']).read_text()
    assert '不是相对母体的应变增量' in text and 'candidate-b' in text
    serialized = (root / report['report_json']).read_text()
    assert len(serialized.splitlines()) > 2
    assert max(map(len, serialized.splitlines())) < 2000
    payload = json.loads(serialized)
    assert payload['md_admission']['state'] == 'not_evaluated'
    assert payload['md_admission']['rejection_reasons'] == []
    assert payload['execution']['scope'] == 'batch'
    assert payload['execution']['return_code'] == 2
    assert payload['evaluation']['candidates'][1]['gate']['covdock_gate'] == 'C'
    assert 'C/C' in text and '不是单个候选的退出码' in text
    assert '尚未验证，不是已证明没有抑制活性' in text
    assert '满足本次入选条件' in text and '后续复核可用' not in text
    assert '仍可审阅现有证据' in text and '不要求一律达到 A 组' in text
    assert 'not a negative activity measurement' in payload['result_state_semantics']['validated_inhibitor_false']
    def unavailable(*args, **kwargs):
        raise covdock_readback.CovDockReadbackUnavailable('artificial runtime unavailable')
    monkeypatch.setattr(refinement_output, '_covdock_sdk', unavailable)
    assert evaluate(case, result, reuse=True) == review
    assert freeze(root, state) == report
    with pytest.raises(covdock_readback.CovDockReadbackUnavailable):
        evaluate(case, result)
    reject_evidence(state, covdock_readback.CovDockReadbackUnavailable('unavailable'))
    assert 'report' not in state['covdock_execution']
    assert state['covdock_execution']['output_validation'] == 'READBACK_RUNTIME_REQUIRED'
    reject_evidence(state, ValueError('changed evidence'))
    assert 'readback_wait' not in state['covdock_execution']
    monkeypatch.setattr(refinement_output, '_covdock_sdk', sdk)
    retain(case, evaluate(case, result))
    assert freeze(root, state) == report
    (root / report['report_markdown']).write_text('tampered')
    with pytest.raises(ValueError):
        freeze(root, state)


def test_self_consistent_output_cannot_change_approved_thresholds(case):
    result, _ = case[-1](policy_override={'hard_max_strain': 99.0})
    with pytest.raises(ValueError, match='thresholds differ'):
        evaluate(case, result)


@pytest.mark.parametrize('change', ['unregistered', 'reader_changed', 'wrong_attachment', 'missing_asset', 'sdk_disagrees'])
def test_source_and_reader_mismatches_are_not_accepted(case, monkeypatch, change):
    root, _, spec, _, build = case
    result, sdk = build()
    if change == 'unregistered':
        result.artifacts = [item for item in result.artifacts if not item.path.endswith('_receptor_prepared.mae')]
    elif change == 'reader_changed':
        spec.parameters['covdock_readback']['sources']['invented'] = '0' * 64
    elif change == 'wrong_attachment':
        spec.parameters['covdock_readback']['runtime']['attachment_residue'] = 'A:773'
    elif change == 'missing_asset':
        (root / 'runtime/run').unlink()
    else:
        def wrong(*args):
            value = copy.deepcopy(sdk(*args))
            value['audits'][0]['rows'][0]['covalent_bond_length_angstrom'] = 2.8
            return value
        monkeypatch.setattr(refinement_output, '_covdock_sdk', wrong)
    expected = covdock_readback.CovDockReadbackUnavailable if change == 'missing_asset' else ValueError
    with pytest.raises(expected):
        evaluate(case, result)


def test_cached_review_witness_is_independent_of_mutable_state(case):
    result, _ = case[-1]()
    review = evaluate(case, result)
    retain(case, review)
    case[3]['covdock_execution']['evaluation']['validated_inhibitor'] = True
    with pytest.raises(ValueError, match='immutable witness'):
        evaluate(case, result, reuse=True)


def test_all_engine_failures_keep_full_denominator_without_reopening_fake_poses(case, monkeypatch):
    root, _, _, state, build = case
    result, _ = build(all_failed=True)
    def forbidden(*args):
        pytest.fail('All-failed batch must not claim or read successful structures')
    monkeypatch.setattr(refinement_output, '_covdock_sdk', forbidden)
    review = evaluate(case, result)
    assert review['candidate_count'] == review['failed_candidate_count'] == 2
    assert review['measured_candidate_count'] == 0 and review['effective_receptor_check'] is None
    assert review['assessment'] == 'evidence_insufficient'
    assert review['output_validation'].endswith('NO_SUCCESSFUL_STRUCTURES')
    assert all(row['measurements'] is None for row in review['candidates'])
    retain(case, review)
    report = freeze(root, state)
    assert report['candidate_count'] == 2 and not report['md_ready']
    assert '未进行厂商准备后受体的独立读回核验' in (root / report['report_markdown']).read_text()
    from covscreen.ligand_workflow import outcome
    state['stage'] = 'COVDOCK_EXECUTION_REVIEW'
    result = outcome(state)
    assert result['assessment']['state'] == 'evidence_insufficient'
    assert 'no successful structures' in result['assessment']['reason']


@pytest.mark.parametrize('field,value', [('thresholds_calibrated', True), ('strain_source_field', 'parent_delta')])
def test_policy_metadata_cannot_relabel_provisional_measurements(case, field, value):
    root, manager, spec, _, build = case
    build()
    gate = json.loads((manager.registry.job_dir(spec.run_id) / 'artifacts/stage5_gate_manifest.json').read_text())
    gate['policy'][field] = value
    with pytest.raises(ValueError, match='provisional policy or strain definition'):
        approved_policy(spec, gate)
