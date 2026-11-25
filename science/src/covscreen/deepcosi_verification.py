"""Replay retained prediction identities and scores without running DeepCoSI."""
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile

from .jobs import sha256_file
from .layout import science_script


def verify_prediction_outputs(output, loaded, parameters, records):
    output = Path(output).resolve()
    if output / 'site-prediction.json' not in records:
        raise ValueError('Prediction manifest is not a registered artifact')
    for path in output.rglob('*'):
        if path.is_symlink():
            raise ValueError('Prediction output contains symlinked evidence')
    # Legacy jobs registered only this manifest. Its frozen hashes bind the
    # normalized structure, residue mapping, raw scores and candidate pockets;
    # the checks below independently rederive identities and compare every row.
    # Auxiliary logs/model copies are not promoted to verified scientific data.
    script = science_script('run_deepcosi_site_prediction.py')
    module_spec = importlib.util.spec_from_file_location('_deepcosi_identity_replay', script)
    runner = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(runner)
    manifest = json.loads((output / 'site-prediction.json').read_text())
    if manifest.get('schema') != runner.PREDICTION_SCHEMA or manifest.get('status') != 'COMPLETED':
        raise ValueError('Prediction manifest is not complete')
    acquisition = loaded['acquisition']
    mapping, = acquisition['acquisition']['mappings']
    chain = mapping['auth_asym_id']
    model = acquisition['selection'].get('model') or 1
    directory = loaded['acquisition_directory']
    source = directory / 'structure.pdb'
    normalized = output / 'normalized-selected-chain.pdb'
    with tempfile.TemporaryDirectory(prefix='deepcosi-verify-') as temporary:
        rebuilt = Path(temporary) / 'normalized.pdb'
        residue_map = runner._normalize_structure(source, rebuilt, chain, model)
        if rebuilt.read_bytes() != normalized.read_bytes():
            raise ValueError('Normalized coordinates differ from frozen receptor')
    map_path = output / 'residue-number-mapping.json'
    expected_map = {'schema': 'deepcosi-residue-map-v1', 'source_sha256': sha256_file(source),
        'normalized_sha256': sha256_file(normalized), 'author_chain': chain, 'model': model,
        'normalized_chain': 'A', 'residues': {str(k): v for k, v in residue_map.items()}}
    if json.loads(map_path.read_text()) != expected_map:
        raise ValueError('Residue mapping differs from frozen receptor')
    candidate, = loaded['identity']['identity']['candidates']
    target = {'gene': loaded['identity']['plan']['gene'], 'taxon': loaded['identity']['plan']['taxon'],
              'uniprot': candidate['uniprot_id']}
    structure = {'entry': acquisition['acquisition']['plan']['entry'], 'model': model, 'author_chain': chain,
        'input_sha256': sha256_file(source), 'normalized_input_sha256': sha256_file(normalized),
        'residue_mapping_path': map_path.name, 'residue_mapping_sha256': sha256_file(map_path)}
    if manifest['target'] != target or manifest['structure'] != structure:
        raise ValueError('Prediction target or structure identity mismatch')
    if manifest['parents'] != {'receptor_input_artifact_id': parameters['receptor_input_artifact_id'],
                               'target_acquisition_manifest_sha256': parameters['manifest_sha256']}:
        raise ValueError('Prediction acquisition parent mismatch')
    raw = output / 'raw-deepcosi'
    validation = json.loads((raw / 'validation.json').read_text())
    if validation.get('status') != 'passed':
        raise ValueError('DeepCoSI runtime validation failed')
    for key in ('entry_sha256', 'model_sha256'):
        if validation.get(key) != parameters['runtime_registration'][key] or manifest['runtime'].get(key) != validation.get(key):
            raise ValueError('DeepCoSI runtime identity mismatch')
    canonical = runner._canonical_map(json.loads((directory / f"entity_{mapping['entity_id']}.json").read_text()),
        json.loads((directory / f"chain_{mapping['label_asym_id']}.json").read_text()), mapping['uniprot'])
    rows = runner.collect_candidates(raw, normalized, residue_map, canonical, validation, chain, output)
    if manifest['candidates'] != rows:
        raise ValueError('Prediction candidates differ from raw scores, pockets or canonical mapping')
    if (manifest.get('candidate_count') != len(rows)
            or manifest.get('scored_candidate_count') != sum(r['prediction_status'] == 'SCORED' for r in rows)
            or manifest.get('failure_count') != sum(r['prediction_status'] == 'FAILED' for r in rows)
            or manifest.get('raw_result_sha256') != sha256_file(raw / 'build/prediction_result.csv')):
        raise ValueError('Prediction count or raw result mismatch')
    if not rows or any(r['prediction_status'] != 'SCORED' for r in rows):
        raise ValueError('Prediction incomplete: missing or failed cysteine scores')
    from .structure_coverage import mapped_coordinate_coverage
    full_mapping = runner._canonical_map(
        json.loads((directory / f"entity_{mapping['entity_id']}.json").read_text()),
        json.loads((directory / f"chain_{mapping['label_asym_id']}.json").read_text()),
        mapping['uniprot'], cysteine_only=False)
    coverage = mapped_coordinate_coverage(residue_map, full_mapping, len(loaded['canonical_sequence']))
    coverage.update(model=model, author_chain=chain, entry=structure['entry'],
        source_sha256=structure['input_sha256'], residue_mapping_sha256=sha256_file(map_path),
        canonical_sequence_sha256=hashlib.sha256(loaded['canonical_sequence'].encode()).hexdigest())
    return coverage


def verify_deepcosi(root, spec, records):
    from .receptor_input import load_receptor_preparation_input
    try:
        loaded = load_receptor_preparation_input(root, spec.parameters['receptor_input_artifact_id'], spec.parameters['manifest_sha256'])
        coverage = verify_prediction_outputs(Path(root) / spec.output_directory / 'site-prediction', loaded, spec.parameters, records)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        return {'errors': ['deepcosi_verification_failed:' + str(error)]}
    return {'errors': [], 'structure_coverage': coverage}
