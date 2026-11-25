import hashlib
import json
from pathlib import Path

import pytest

from covscreen.receptor_geometry import audit_receptor_geometry


ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / 'reports/library-first-site-acquisition-20261014'
FOLDER = REPORT / 'receptor-relaxation-regression-v4'
QUALITY = REPORT / 'receptor-relaxation-regression-v4-quality.json'
SOURCE = ROOT / 'science/projects/egfr-c797/ligand_first/source_structures/1M17.pdb'


def test_real_unrestrained_regression_passes_independent_geometry():
    result = audit_receptor_geometry(FOLDER,
        hashlib.sha256((FOLDER / 'receipt.json').read_bytes()).hexdigest(),
        QUALITY, hashlib.sha256(QUALITY.read_bytes()).hexdigest(), SOURCE)
    assert result['status'] == 'geometry_passed_environment_review_pending'
    assert all(check['violation_count'] == 0 for check in result['distance_checks'].values())
    assert result['aromatic_planarity_violations'] == []
    assert result['vdw_clashes'] == []
    assert result['reactive_site_prereaction_state']['status'] == 'pass'
    assert result['receptor_prepared'] is False


def test_geometry_refuses_failed_prior_quality(tmp_path):
    value = json.loads(QUALITY.read_text())
    value['status'] = 'heavy_atom_quality_failed'
    path = tmp_path / 'quality.json'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='must pass first'):
        audit_receptor_geometry(FOLDER,
            hashlib.sha256((FOLDER / 'receipt.json').read_bytes()).hexdigest(),
            path, hashlib.sha256(path.read_bytes()).hexdigest(), SOURCE)


def test_v2_full_artifact_replay_preserves_scope_and_rejects_forged_identity(tmp_path, monkeypatch):
    """Synthetic v2 metadata over historical coordinates; no new engine evidence."""
    import os
    import shutil
    from copy import deepcopy
    from covscreen.receptor_relaxation import RELAXATION_POLICY_V2
    from covscreen.receptor_quality import audit_restrained_receptor
    from covscreen.receptor_geometry import audit_restrained_geometry
    history = Path(os.environ.get('COVSCREEN_RECEPTOR_FIXTURE_ROOT', '/missing-fixture'))
    origin = json.loads((history / 'fixture-origin.json').read_text())
    for item in origin['files']:
        assert hashlib.sha256((history / item['path']).read_bytes()).hexdigest() == item['sha256']
    pipeline = history / 'state/jobs/20261014T072946-library-first-receptor-preparation-cd6429155a/artifacts/receptor-preparation'
    source = history / 'state/target_acquisition/3fe7754be1b14546a7141aa26cebde6b/structure.pdb'
    parent = pipeline / 'heavy'
    folder = tmp_path / 'synthetic-v2'
    shutil.copytree(pipeline / 'relaxation', folder)
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = json.loads((folder / 'receipt.json').read_text())
    for name in ('atom-mapping.json', 'heavy-atom-mapping.json'):
        path = folder / name
        mapping = json.loads(path.read_text())
        for atom in mapping['atoms']:
            if atom['original_serial'] is not None:
                atom['source_xyz_angstrom'] = list(atom['xyz_angstrom'])
                atom['coordinate_origin'] = 'relaxed_observed_atom'
        path.write_text(json.dumps(mapping))
        receipt['artifacts'][name] = sha(path)
    policy = deepcopy(RELAXATION_POLICY_V2)
    receipt.update(schema='receptor-relaxation-receipt-v2',
        status='position_restrained_relaxation_completed_quality_pending',
        artificial_forces_in_final_stage=True, policy=policy,
        policy_sha256=hashlib.sha256(json.dumps(policy, sort_keys=True,
                             separators=(',', ':')).encode()).hexdigest())
    receipt['stages'][-1].update(name='amber_position_restrained',
        artificial_position_restraint=True, observed_atoms_fixed=False)
    (folder / 'receipt.json').write_text(json.dumps(receipt))
    digest = sha(folder / 'receipt.json')
    identity = audit_restrained_receptor(folder, digest, parent, source)
    assert identity['status'] == 'identity_and_stereochemistry_passed_geometry_pending'
    assert identity['observed_motion_acceptance'] == 'not_evaluated'
    assert identity['observed_coordinate_displacements']['maximum_displacement_angstrom'] < 1e-9
    quality = tmp_path / 'quality.json'
    quality.write_text(json.dumps(identity))
    result = audit_restrained_geometry(folder, digest, quality, sha(quality), source, parent)
    assert result['schema'] == 'receptor-geometry-audit-v2'
    assert result['status'] == 'geometry_passed_environment_review_pending'
    assert result['receptor_prepared'] is False
    assert result['observed_motion_acceptance'] == 'not_evaluated'
    assert 'observed_origin_pair_violations' in result['distance_checks']['internal']
    # Continue through real registered-output checking, replay and report sealing.
    # Only current-study input binding is isolated from this historical fixture.
    from types import SimpleNamespace
    from covscreen import ligand_receptor_input
    from covscreen.ligand_receptor_evidence import evaluate_restrained
    from covscreen.ligand_receptor_report import freeze
    bundle = tmp_path / 'registered'
    bundle.mkdir()
    shutil.copytree(folder, bundle / 'relaxation')
    shutil.copytree(parent, bundle / 'heavy')
    (bundle / 'identity-quality.json').write_text(json.dumps(identity))
    (bundle / 'geometry-quality.json').write_text(json.dumps(result))
    pipeline_record = {'schema': 'receptor-preparation-pipeline-v2',
        'status': 'observed_motion_review_pending', 'receptor_prepared': False,
        'target_acquisition_sha256': 'synthetic-source-binding',
        'relaxation_policy': RELAXATION_POLICY_V2,
        'artifacts': {str(p.relative_to(bundle)): sha(p) for p in bundle.rglob('*') if p.is_file()}}
    manifest = bundle / 'pipeline.json'
    manifest.write_text(json.dumps(pipeline_record))
    spec = SimpleNamespace(run_id='synthetic-v2', parameters={'relaxation_policy': RELAXATION_POLICY_V2})
    registered = SimpleNamespace(artifacts=[SimpleNamespace(path=str(p.relative_to(tmp_path)),
        run_id=spec.run_id, sha256=sha(p), size_bytes=p.stat().st_size)
        for p in bundle.rglob('*') if p.is_file()])
    monkeypatch.setattr(ligand_receptor_input, 'validate_binding',
        lambda *args: {'acquisition_directory': source.parent,
            'acquisition': json.loads((source.parent / 'target_acquisition.json').read_text())})
    state = {'workflow_id': 'synthetic', 'design_id': 'synthetic', 'target': {},
        'vs_receptor': {'receptor_input': {'manifest_sha256': 'synthetic-source-binding'}}}
    evaluation = evaluate_restrained(tmp_path, state, spec, registered, bundle, manifest, pipeline_record)
    assert evaluation['receptor_prepared'] is False
    state['vs_receptor']['receptor_preparation'] = {
        'evaluation_verification': 'verified', 'evaluation': evaluation}
    report = freeze(tmp_path, state)
    assert freeze(tmp_path, state) == report
    assert '受体尚未准入' in (tmp_path / report['report_markdown']).read_text()
    from covscreen import receptor_admission
    from covscreen.ligand_receptor_evidence import freeze_review
    from covscreen.receptor_environment_review import freeze as freeze_motion, MOTION_CONCERNS
    witness = freeze_review(tmp_path, evaluation)
    review_inventory = {witness['path']: witness['manifest_sha256']}
    review_binding = {'workflow_id': 'synthetic', 'run_id': spec.run_id,
        'evaluation_sha256': witness['manifest_sha256']}
    citation = {'path': witness['path'], 'sha256': witness['manifest_sha256'],
        'locator': '/result/quality/observed_coordinate_displacements/maximum_displacement_angstrom',
        'observation': 'Historical coordinates with synthetic policy metadata; software test only.'}
    assessments = {name: {'decision': 'evidence_insufficient',
        'rationale': 'This synthetic fixture is not evidence of a successful scientific preparation.',
        'evidence': [citation], 'limitations': ['No actual v2 engine execution.']}
        for name in MOTION_CONCERNS}
    review_ref = freeze_motion(tmp_path, review_binding, review_inventory, assessments,
        scope='restrained-receptor')
    admission_args = (source.parent, 'synthetic-source-binding', '', 9606,
        bundle / 'heavy', bundle / 'relaxation', digest,
        bundle / 'identity-quality.json', sha(bundle / 'identity-quality.json'),
        bundle / 'geometry-quality.json', sha(bundle / 'geometry-quality.json'), tmp_path / 'admitted')
    with pytest.raises(ValueError, match='all motion review concerns'):
        receptor_admission.admit_restrained_receptor(tmp_path, review_ref, review_binding,
            review_inventory, *admission_args)
    for item in assessments.values():
        item['decision'] = 'suitable_for_exploratory_receptor_preparation'
    review_ref = freeze_motion(tmp_path, review_binding, review_inventory, assessments,
        scope='restrained-receptor')
    # Historical upstream construct policy is outside this synthetic admission test.
    monkeypatch.setattr(receptor_admission, 'load_target_acquisition',
        lambda *args: json.loads((source.parent / 'target_acquisition.json').read_text()))
    admitted = receptor_admission.admit_restrained_receptor(tmp_path, review_ref, review_binding,
        review_inventory, *admission_args)
    assert admitted['schema'] == 'prepared-receptor-artifact-v2'
    assert admitted['motion_review']['reference'] == review_ref
    assert admitted['binding_site_preservation_validated'] is False
    assert admitted['campaign_ready'] is False
    assert all(sha(tmp_path / 'admitted' / name) == checksum
        for name, checksum in admitted['artifacts'].items())
    from covscreen.receptor_site_context import assess_receptor_site_context, _load_prepared
    prepared = tmp_path / 'admitted'
    prepared_sha = sha(prepared / 'prepared-receptor.json')
    with pytest.raises(ValueError, match='bound review root'):
        _load_prepared(prepared, prepared_sha, 'synthetic-source-binding')
    site = assess_receptor_site_context(source.parent, 'synthetic-source-binding',
        json.loads((source.parent / 'target_acquisition.json').read_text()),
        prepared, prepared_sha, tmp_path / 'site-context', review_root=tmp_path)
    assert site['schema'] == 'receptor-site-context-artifact-v2'
    assert site['coordinate_scope'] == 'position_restrained_computational_proposal'
    assert site['motion_review']['reference'] == review_ref
    assert site['binding_site_suitability_validated'] is False
    assert site['prepared_isolated_protein']['protein_shell']
    from covscreen.ligand_receptor_admission import materialize
    delivered = materialize(tmp_path, state,
        {'run_id': spec.run_id, 'motion_review': review_ref}, evaluation,
        {'acquisition_directory': source.parent,
         'acquisition': json.loads((source.parent / 'target_acquisition.json').read_text()),
         'canonical_sequence': '', 'taxon': 9606}, bundle)
    assert delivered['receptor_prepared'] is True
    assert delivered['md_ready'] is False and delivered['covdock_ready'] is False
    assert delivered['proposal_review'] == witness
    assert delivered['result']['inventory'][str(manifest.relative_to(tmp_path))] == sha(manifest)
    assert delivered['result']['identity']['author_residue'] == '773'
    receptor = delivered['result']['receptor']
    assert sha(tmp_path / receptor['path']) == receptor['sha256']
    assert delivered['result']['inventory'][review_ref['path']] == review_ref['manifest_sha256']
    state['vs_receptor']['receptor_preparation']['evaluation'] = delivered
    delivered_report = freeze(tmp_path, state)
    assert '已依据绑定审阅生成探索性受体产物' in (tmp_path / delivered_report['report_markdown']).read_text()
    from covscreen import ligand_receptor_execution
    from covscreen.ligand_vs_receptor import augment
    # Task approval/replay lifecycle has separate tests; use the just-replayed
    # delivery here to exercise real VS identity checks and coordinate projection.
    monkeypatch.setattr(ligand_receptor_execution, 'observe', lambda *args: {})
    monkeypatch.setattr(ligand_receptor_execution, 'review', lambda *args, **kwargs: {})
    state['vs_receptor']['source_input'] = {'artifact_id': 'synthetic-source'}
    state['vs_receptor']['receptor_preparation'].update(run_id=spec.run_id,
        evaluation_artifact=freeze_review(tmp_path, delivered))
    acquired = json.loads((source.parent / 'target_acquisition.json').read_text())
    payload = {'source_site_mapping': acquired['acquisition']['mappings'][0],
        'lineage_sha256': {}, 'review_requirements': {}}
    vs_files = {'receptor-inventory.json': (parent / 'inventory.json').read_bytes()}
    augment(tmp_path, state, payload, vs_files)
    assert payload['receptor_prepared'] is True
    assert payload['lineage_sha256'][review_ref['path']] == review_ref['manifest_sha256']
    assert payload['review_requirements']['preparation_is_not_environment_suitability'] is True
    before = [line[30:54] for line in (tmp_path / receptor['path']).read_text().splitlines()
        if line.startswith('ATOM')]
    after = [line[30:54] for line in vs_files['prepared-vs-receptor.pdb'].decode().splitlines()
        if line.startswith('ATOM')]
    assert before == after
    identity['observed_coordinate_displacements']['atom_count'] -= 1
    quality.write_text(json.dumps(identity))
    with pytest.raises(ValueError, match='independent replay'):
        audit_restrained_geometry(folder, digest, quality, sha(quality), source, parent)
