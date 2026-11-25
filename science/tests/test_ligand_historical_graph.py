"""Synthetic source chain through real selection, approval and graph ledger code."""
import csv
import hashlib
import json
from pathlib import Path

import pytest

from covscreen.md_historical_source import freeze, sha
from covscreen.md_candidate_pose import load_candidate_pose_input
from covscreen.ligand_workflow import navigate, directory, DESIGN
from covscreen.mcp.server import dispatch
from covscreen.workflow_store import load
from covscreen.execution import JobManager
from covscreen.target_identity import target_plan, parse_targets
from test_md_historical_source import sources, write
from test_ligand_md_selection import runtime
from test_ligand_md_graph_execution import arguments, approve
from test_workflow_md_execution import register


@pytest.fixture
def historical(tmp_path, sources, monkeypatch):
    # A real public sequence, with explicitly artificial structure bytes.
    raw = (Path(__file__).parent / 'fixtures/target_identity/egfr.json').read_bytes()
    payload = json.loads(raw)
    identity = parse_targets(payload, 'EGFR', 9606, len(payload['results']))
    candidate = identity['candidates'][0]
    canonical = candidate.pop('sequence')
    fasta = ('>P00533 EGFR taxon=9606 canonical\n' + canonical + '\n').encode()
    candidate.update(sequence_path='P00533.fasta', fasta_sha256=hashlib.sha256(fasta).hexdigest())
    target_id = 'target-' + 'a' * 32
    target_dir = tmp_path / 'state/target_identity' / target_id[7:]
    target_dir.mkdir(parents=True)
    (target_dir / 'uniprot.json').write_bytes(raw)
    (target_dir / 'P00533.fasta').write_bytes(fasta)
    plan = target_plan('EGFR', 9606)
    target = {'schema': 'target-identity-v1', 'status': 'identity_resolved_site_assessment_required',
        'plan': plan, 'sources': [{'url': plan['uniprot_url'], 'path': 'uniprot.json',
            'sha256': hashlib.sha256(raw).hexdigest(), 'total_results_header': str(len(payload['results']))}],
        'identity': identity, 'site_assessment': {'selected_site': None, 'reactivity_validated': False},
        'campaign_ready': False, 'calculation_submitted': False}
    write(target_dir / 'target_identity.json', target)
    reference = {'target_artifact_id': target_id, 'target_manifest_sha256': sha(target_dir / 'target_identity.json')}
    sources['construct_fasta'].write_text('>artificial-source-construct\n' + canonical[695:1016] + '\n')
    source = json.loads(sources['source_manifest'].read_text())
    source['records'][0].update(sequence_sha256=sha(sources['construct_fasta']), construct_start=696,
        construct_end=1016, target_residue=797, protein_chain='A', title='Synthetic candidate fixture')
    with sources['candidate_table'].open() as stream:
        rows = list(csv.DictReader(stream))
    rows[0].update(af3_status='SUCCESS', engine_status='SUCCESS', physical_validity='PASS',
        standardized_smiles='C=CC(=O)N', warhead_class='acrylamide')
    with sources['candidate_table'].open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    source['source_table_sha256'] = sha(sources['candidate_table'])
    write(sources['source_manifest'], source)
    write(sources['source_inspection'], {'records': source['records'], 'source_manifest_sha256': sha(sources['source_manifest'])})
    for role in ('loop_started', 'loop_finished'):
        value = json.loads(sources[role].read_text()); value['sequence_sha256'] = sha(sources['construct_fasta']); write(sources[role], value)
    clean = json.loads(sources['clean_qc'].read_text())
    clean.update(status='LOOP_GRAPH_QC_PASSED_PENDING_RELAXATION', warnings=[])
    write(sources['clean_qc'], clean)
    artifact = freeze(tmp_path, sources, system_id='ligand_066', compound_id='fixture-066', input_sdf_index=2)
    current = navigate(tmp_path, 'start', request_id='historical-graph', target='EGFR', site='C797', taxon=9606)
    workflow_id = current['workflow']['workflow_id']
    navigate(tmp_path, 'attach_md_source', workflow_id=workflow_id, expected_revision=1,
        source_artifact_id=artifact['artifact_id'], source_manifest_sha256=artifact['manifest_sha256'],
        reason='Software-only historical candidate input')
    folder = directory(tmp_path, workflow_id)
    key = tmp_path / 'test-key'
    key.write_text('isolated-historical-graph-test-key-not-production-123456'); key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    runtime(tmp_path)
    monkeypatch.setattr(JobManager, 'submit', register)
    return (tmp_path, folder), reference


def prepare(historical):
    fixture, reference = historical
    return dispatch(fixture[0], DESIGN, arguments(fixture, 'prepare_historical_md_candidate',
        **reference, reason='Inspect this preserved candidate using fresh native checks'))


def test_historical_selection_and_plan_keep_source_without_fake_covdock(historical):
    fixture, reference = historical
    value = prepare(historical)
    assert value['covdock_execution'] is None and value['design_id'] is None
    assert value['md_candidate_selection']['state'] == 'selected'
    assert value['historical_md_source']['canonical_scope_verified'] is False
    selected = value['historical_md_source']['md_selection']
    loaded = load_candidate_pose_input(fixture[0], selected['artifact_id'], selected['manifest_sha256'])
    assert loaded['manifest']['target']['canonical_cys'] == 797
    assert loaded['manifest']['target']['entry_id'] == 'historical:historical-test'
    assert loaded['manifest']['parents']['target_identity_sha256'] == reference['target_manifest_sha256']
    planned = dispatch(fixture[0], DESIGN, arguments(fixture, 'check_md_graph_runtime'))
    spec = planned['historical_md_source']['md_graph_plan']['spec']
    assert spec['project_id'] == planned['workflow']['workflow_id']
    assert spec['engine'] == 'library-first-md-adduct-admission'
    assert spec['resource_budget']['gpu_count'] == 0
    with pytest.raises(ValueError, match='approval'):
        dispatch(fixture[0], DESIGN, arguments(fixture, 'start_md_graph', human_approved=True))
    started = dispatch(fixture[0], DESIGN, approve(fixture))
    run_id = started['md_graph_execution']['run_id']
    assert started['workflow']['stage'] == 'MD_GRAPH_RUNNING'
    assert 'covalent_ligand_evidence' not in started['workflow']['tools']
    assert started['covdock_execution'] is None
    with pytest.raises(ValueError, match='unexecuted'):
        prepare(historical)
    observed = dispatch(fixture[0], DESIGN, arguments(fixture, 'md_graph_status'))
    assert observed['md_graph_execution']['run_id'] == run_id
    assert len(list((fixture[0] / 'state/jobs').glob('*/spec.json'))) == 1  # only the newly registered graph


def test_historical_source_change_invalidates_selection_and_plan(historical):
    fixture, _ = historical
    prepare(historical)
    planned = dispatch(fixture[0], DESIGN, arguments(fixture, 'check_md_graph_runtime'))
    source = planned['historical_md_source']
    path = fixture[0] / 'state/historical_md_sources' / source['manifest_sha256'][:32] / 'adduct-input.mae'
    original = path.read_bytes(); path.write_bytes(b'changed historical adduct')
    rejected = navigate(fixture[0], 'status', workflow_id=planned['workflow']['workflow_id'])
    assert rejected['historical_md_source']['md_selection_verification'] == 'evidence_rejected'
    assert 'start_md_graph' not in rejected['workflow']['tools'].get(DESIGN, {}).get('actions', [])
    path.write_bytes(original)
    restored = navigate(fixture[0], 'status', workflow_id=planned['workflow']['workflow_id'])
    assert restored['historical_md_source']['md_selection_verification'] == 'verified'


def test_target_scope_and_copied_target_tampering_rejected(historical):
    fixture, _ = historical
    prepared = prepare(historical)
    selected = prepared['historical_md_source']['md_selection']
    loaded = load_candidate_pose_input(fixture[0], selected['artifact_id'], selected['manifest_sha256'])
    (loaded['directory'] / 'target/P00533.fasta').write_text('>wrong\nAAA\n')
    with pytest.raises(ValueError, match='FASTA differs'):
        load_candidate_pose_input(fixture[0], selected['artifact_id'], selected['manifest_sha256'])


def test_graph_policy_is_frozen_and_old_manifest_can_still_be_read(historical):
    from covscreen.md_historical_pose import load as load_pose, encoded, GRAPH_POLICY
    fixture, _ = historical
    prepared = prepare(historical)
    selected = prepared['historical_md_source']['md_selection']
    loaded = load_candidate_pose_input(fixture[0], selected['artifact_id'], selected['manifest_sha256'])
    manifest = dict(loaded['manifest'])
    assert manifest.pop('historical_graph_policy') == GRAPH_POLICY
    # Reconstruct an explicitly legacy fixture and verify its original strict policy.
    path = loaded['directory'] / 'candidate-pose-input.json'
    path.write_bytes(encoded(manifest))
    legacy_hash = sha(path)
    assert legacy_hash != selected['manifest_sha256']
    assert 'historical_graph_policy' not in load_pose(loaded['directory'], legacy_hash)['manifest']
    manifest['historical_graph_policy'] = 'allow-all-movement'
    path.write_bytes(encoded(manifest))
    with pytest.raises(ValueError, match='unsupported historical graph policy'):
        load_pose(loaded['directory'], sha(path))


def test_historical_graph_requires_native_witness_then_feeds_parameterization(historical):
    from test_workflow_md_execution import completed_graph_output
    fixture, _ = historical
    prepared = prepare(historical)
    selected = prepared['historical_md_source']['md_selection']
    dispatch(fixture[0], DESIGN, arguments(fixture, 'check_md_graph_runtime'))
    started = dispatch(fixture[0], DESIGN, approve(fixture))
    run_id = started['md_graph_execution']['run_id']
    output = completed_graph_output(fixture[0], selected, run_id)
    with pytest.raises(ValueError, match='Historical native'):
        dispatch(fixture[0], DESIGN, arguments(fixture, 'md_graph_status'))
    assert load(fixture[1])['md_graph_execution']['verification'] == 'evidence_rejected'
    native = output / 'native-handoff'
    # Explicit artificial witness: tests the ledger/contract, not native science.
    original = load_candidate_pose_input(fixture[0], selected['artifact_id'], selected['manifest_sha256'])['manifest']
    handoff = json.loads((native / 'candidate-md-handoff.json').read_text())
    audit = {'schema': 'historical-adduct-native-audit-v1', 'candidate_id': original['candidate_id'],
        'compound_id': original['compound_id'], 'candidate_pose_manifest_sha256': selected['manifest_sha256'],
        'source_manifest_sha256': original['parents']['historical_source_sha256'],
        'target_manifest_sha256': original['parents']['target_identity_sha256'],
        'adduct_sha256': handoff['artifacts']['adduct.mae'], 'parameterization_completed': False, 'md_system_ready': False,
        'canonical_site': 797, 'author_chain': 'A', 'author_residue': 797,
        **{k: True for k in ('original_pose_identity_verified', 'original_heavy_graph_preserved',
            'cleanup_retained_graph_verified', 'native_coordinate_mapping_verified',
            'peptide_connectivity_verified', 'new_loop_clash_check_passed')}}
    write(native / 'historical-structure-audit.json', audit)
    handoff['historical_structure_audit_sha256'] = sha(native / 'historical-structure-audit.json')
    write(native / 'candidate-md-handoff.json', handoff)
    receipt = json.loads((output / 'md-adduct-admission.json').read_text())
    receipt['parents']['native_handoff_manifest_sha256'] = sha(native / 'candidate-md-handoff.json')
    write(output / 'md-adduct-admission.json', receipt)
    manager = JobManager(fixture[0]); result = manager.registry.load_result(run_id)
    result.artifacts = [manager.registry.register_file(run_id, p, 'other') for p in output.rglob('*') if p.is_file()]
    manager.registry.write_result(result)
    accepted = dispatch(fixture[0], DESIGN, arguments(fixture, 'md_graph_status'))
    assert accepted['md_graph_execution']['verification'] == 'verified'
    planned = dispatch(fixture[0], DESIGN, arguments(fixture, 'plan_md_parameterization'))
    assert planned['workflow']['stage'] == 'MD_PARAMETERIZATION_PLAN_READY'
    assert planned['md_parameterization']['selection']['candidate_id'] == selected['candidate_id']
    assert planned['covdock_execution'] is None
