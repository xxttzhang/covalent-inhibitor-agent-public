"""Artificial document-chain tests; these bytes are not scientific structures."""
import csv
import json

import pytest

from covscreen.md_historical_source import FILES, freeze, load_source, sha
from covscreen.ligand_workflow import navigate


def write(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def sources(tmp_path):
    folder = tmp_path / 'history'
    folder.mkdir()
    files = {role: folder / name for role, name in FILES.items()}
    job = tmp_path / 'state/jobs/historical-test'
    job.mkdir(parents=True)
    for role in ('pose_file', 'covdock_manifest'):
        files[role] = job / FILES[role]
    for role, path in files.items():
        path.write_text('ARTIFICIAL SOURCE ' + role)
    native = {'status': 'completed', 'pose_files': [str(files['pose_file'])],
              'gate_quality_gates': {'document_fixture': True}}
    write(files['covdock_manifest'], native)
    spec = {'run_id': 'historical-test', 'route': 'ligand-first', 'engine': 'schrodinger-covdock-lo',
            'project_id': 'test', 'attempt': 1}
    write(files['covdock_spec'], spec)
    result = {**spec, 'state': 'COMPLETED', 'return_code': 0,
        'artifacts': [{'path': str(files[role].relative_to(tmp_path)), 'run_id': spec['run_id'],
                      'sha256': sha(files[role]), 'size_bytes': files[role].stat().st_size}
                     for role in ('pose_file', 'covdock_manifest')]}
    write(files['covdock_result'], result)
    candidate = {'compound_id': 'fixture-066', 'input_sdf_index': '2', 'covdock_pose_file': str(files['pose_file']),
        'covdock_pose_index': '7', 'coordinate_recheck_source_sha256': sha(files['pose_file'])}
    with files['candidate_table'].open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(candidate))
        writer.writeheader()
        writer.writerow(candidate)
    row = {'system_id': 'ligand_066', 'compound_id': 'fixture-066', 'pose': 7,
        'path': str(files['pose_file']), 'sha256': sha(files['pose_file']),
        'source_mae_sha256': sha(files['extracted_pose']), 'sequence_sha256': sha(files['construct_fasta']),
        'missing_internal_residues': [721]}
    write(files['source_manifest'], {'schema': 'ligand-three-md-source-v1', 'records': [row],
        'source_table': str(files['candidate_table']), 'source_table_sha256': sha(files['candidate_table'])})
    write(files['source_inspection'], {'records': [row], 'source_manifest_sha256': sha(files['source_manifest'])})
    started = {'system_id': 'ligand_066', 'source_sha256': sha(files['extracted_pose']),
        'sequence_sha256': sha(files['construct_fasta']), 'missing_internal_residues': [721]}
    write(files['loop_started'], started)
    write(files['loop_finished'], {**started, 'status': 'LOOP_MODEL_WRITTEN_REQUIRES_QC',
        'returncode': 0, 'output_sha256': sha(files['filled_pose'])})
    qc = {'system_id': 'ligand_066', 'source_sha256': sha(files['extracted_pose']),
        'filled_sha256': sha(files['filled_pose']), 'loop_qc_input_sha256': sha(files['loop_input']),
        'status': 'HISTORICAL_DOCUMENT_ONLY', 'prep_validated': False, 'warnings': ['Synthetic example']}
    write(files['loop_qc'], qc)
    write(files['cleanup'], {'system_id': 'ligand_066', 'source_QC_sha256': sha(files['loop_qc']),
        'source_input_sha256': sha(files['loop_input']), 'output_sha256': sha(files['adduct_input'])})
    write(files['clean_qc'], {**qc, 'upstream_loop_qc_sha256': sha(files['loop_qc']),
        'cleanup_sha256': sha(files['cleanup']), 'loop_qc_input_sha256': sha(files['adduct_input'])})
    return files


def produce(root, sources):
    return freeze(root, sources, system_id='ligand_066', compound_id='fixture-066', input_sdf_index=2)


def test_freeze_replay_idempotence_and_preserve_uncertainty(tmp_path, sources):
    receipt = produce(tmp_path, sources)
    assert produce(tmp_path, sources) == receipt
    loaded = load_source(tmp_path, receipt['artifact_id'], receipt['manifest_sha256'])
    manifest = loaded['manifest']
    assert len(loaded['evidence_files']) == 18
    assert manifest['identity']['recorded_qc']['warnings'] == ['Synthetic example']
    assert manifest['identity']['recorded_qc']['prep_validated'] is False
    assert not any(manifest[k] for k in ('calculation_submitted', 'parameterization_completed',
                                        'native_graph_qc_completed', 'md_system_ready'))
    # Portable replay uses the frozen evidence, not mutable historical paths.
    sources['adduct_input'].unlink()
    assert load_source(tmp_path, receipt['artifact_id'], receipt['manifest_sha256'])['manifest'] == manifest


@pytest.mark.parametrize('role,key', [('cleanup', 'output_sha256'), ('loop_qc', 'source_sha256'),
    ('clean_qc', 'upstream_loop_qc_sha256'), ('loop_finished', 'output_sha256'),
    ('source_inspection', 'source_manifest_sha256')])
def test_changed_chain_link_rejected_before_registration(tmp_path, sources, role, key):
    value = json.loads(sources[role].read_text())
    value[key] = '0' * 64
    write(sources[role], value)
    with pytest.raises(ValueError):
        produce(tmp_path, sources)
    assert not (tmp_path / 'state/historical_md_sources').exists()


def test_registered_pose_mismatch_and_symbolic_link_rejected(tmp_path, sources):
    sources['pose_file'].write_text('DIFFERENT ARTIFICIAL CANDIDATE')
    with pytest.raises(ValueError, match='pose_file differs'):
        produce(tmp_path, sources)
    actual = sources['adduct_input']
    link = actual.with_suffix('.link')
    link.symlink_to(actual)
    sources['adduct_input'] = link
    with pytest.raises(ValueError, match='symbolic links'):
        produce(tmp_path, sources)


def test_outer_manifest_rehash_cannot_hide_internal_binding_change(tmp_path, sources):
    receipt = produce(tmp_path, sources)
    loaded = load_source(tmp_path, receipt['artifact_id'], receipt['manifest_sha256'])
    directory = loaded['directory']
    path = directory / FILES['adduct_input']
    path.write_text('A DIFFERENT ARTIFICIAL ADDUCT')
    manifest = loaded['manifest']
    manifest['sources']['adduct_input'].update(sha256=sha(path), size=path.stat().st_size)
    manifest_path = directory / 'historical-md-source.json'
    write(manifest_path, manifest)
    digest = sha(manifest_path)
    destination = directory.parent / digest[:32]
    directory.rename(destination)
    with pytest.raises(ValueError, match='cleanup chain differs'):
        load_source(tmp_path, 'historical-md-source-' + digest[:32], digest)


def test_workflow_attach_readback_corruption_restore_and_archive(tmp_path, sources):
    receipt = produce(tmp_path, sources)
    initial = navigate(tmp_path, 'start', request_id='history-test', target='EGFR', site='C797', taxon=9606)
    workflow_id = initial['workflow']['workflow_id']
    assert 'attach_md_source' in initial['workflow']['tools']['covalent_ligand_workflow']['actions']
    result = navigate(tmp_path, 'attach_md_source', workflow_id=workflow_id, expected_revision=1,
        source_artifact_id=receipt['artifact_id'], source_manifest_sha256=receipt['manifest_sha256'],
        reason='Continue a documented historical candidate, requiring new native review')
    assert result['workflow']['stage'] == 'HISTORICAL_MD_SOURCE_REVIEW'
    assert result['execution']['task_ids'] == [] and result['compute_submitted'] is False
    assert result['md_candidate_selection']['state'] == 'no_active_selection'
    assert result['covdock_execution'] is None and result['md_graph_execution'] is None
    assert result['historical_md_source']['canonical_scope_verified'] is False
    assert 'covalent_ligand_evidence' not in result['workflow']['tools']
    assert navigate(tmp_path, 'status', workflow_id=workflow_id)['historical_md_source'] == result['historical_md_source']
    path = load_source(tmp_path, receipt['artifact_id'], receipt['manifest_sha256'])['directory'] / FILES['cleanup']
    original = path.read_bytes()
    path.write_bytes(b'changed receipt')
    rejected = navigate(tmp_path, 'status', workflow_id=workflow_id)
    assert rejected['historical_md_source']['verification'] == 'evidence_rejected'
    assert any(b['code'] == 'evidence_rejected' for b in rejected['blockers'])
    path.write_bytes(original)
    restored = navigate(tmp_path, 'status', workflow_id=workflow_id)
    assert restored['historical_md_source']['verification'] == 'source_chain_verified'
    archived = navigate(tmp_path, 'reconsider', workflow_id=workflow_id,
        expected_revision=restored['workflow']['revision'], reason='Archive this unexecuted source')
    assert archived['workflow']['stage'] == 'INTAKE'
    assert archived['historical_md_source'] is None
    assert archived['branches'][-1]['historical_md_source']['artifact_id'] == receipt['artifact_id']


def test_stale_revision_and_source_replacement_are_not_silent(tmp_path, sources):
    receipt = produce(tmp_path, sources)
    initial = navigate(tmp_path, 'start', request_id='history-test', target='EGFR', site='C797', taxon=9606)
    arguments = dict(workflow_id=initial['workflow']['workflow_id'], expected_revision=1,
        source_artifact_id=receipt['artifact_id'], source_manifest_sha256=receipt['manifest_sha256'], reason='Original source choice')
    result = navigate(tmp_path, 'attach_md_source', **arguments)
    with pytest.raises(ValueError, match='STALE_WORKFLOW_REVISION'):
        navigate(tmp_path, 'attach_md_source', **arguments)
    arguments['expected_revision'] = result['workflow']['revision']
    assert navigate(tmp_path, 'attach_md_source', **arguments)['workflow']['revision'] == result['workflow']['revision']
    with pytest.raises(ValueError, match='Reconsider'):
        navigate(tmp_path, 'attach_md_source', **{**arguments, 'reason': 'Silently changed choice'})
