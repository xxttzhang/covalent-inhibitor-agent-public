"""Portable current-study review delivery, distinct from legacy CovDock manifests.

Caller verifies live workflow/ledger evidence before freezing. Portable replay
checks copied evidence and identities; it never performs native graph QC or MD.
"""
import csv
import io
import json
from pathlib import Path
import shutil

from .candidate_delivery import IDENTITY_FIELDS, _text, _read_table, build_candidate_results_manifest
from .jobs import sha256_file
from .md_candidate_delivery import _regular
from .md_candidate_pose import _receptor_parents, _delivered_pose
from .refinement_input import verify_refinement_input
from .pose_identity import exact_pose_index
from .metric_contracts import md_not_evaluated

PROFILE = 'current-study-refinement-reviews-v1'


def projection(source, table, prepared, context, parents):
    """Derive candidate rows from original inputs and separately observed methods."""
    if source.get('schema') != PROFILE or source.get('route') != 'ligand-first':
        raise ValueError('Unsupported current-study review source')
    covdock = source['covdock']
    contract = covdock['input_contract']
    verify_refinement_input(table, contract['input_count'], contract['input_sha256'],
                            contract['identity_sha256'], expected_route='ligand-first')
    if (source['task_id'] != covdock['run_id']
            or covdock['candidate_artifact_id'] != source['candidate_artifact_id']
            or covdock['schema'] != 'ligand-covdock-measurement-review-v1'):
        raise ValueError('Current-study CovDock identity differs')
    identity = _receptor_parents(prepared, parents['prepared_receptor'], context, parents['receptor_context'])
    attachment = identity['author_chain'] + ':' + str(identity['author_residue'])
    if (covdock['attachment_residue'] != attachment or identity['uniprot'] != 'P00533'
            or identity['canonical_cys'] != 797 or source['reaction_type'] != 'Michael Addition'):
        raise ValueError('Current-study receptor/reaction context differs')
    raw = _read_table(table).to_dict(orient='records')
    for row in raw:
        row['input_sdf_index'] = exact_pose_index(row['input_sdf_index'])
    expected = {(row['input_sdf_index'], row['compound_id']) for row in raw}
    rows = {key: {} for key in expected}
    for method in ('covdock', 'af3'):
        review = source[method]
        if review is None and method == 'af3':
            continue
        if method == 'af3':
            mapping = review['target_mapping']['source']
            if (review['schema'] != 'ligand-af3-measurement-review-v1'
                    or review['target_artifact_id'] != source['target_artifact_id']
                    or mapping['entry_id'] != identity['entry_id']
                    or mapping['auth_asym_id'] != identity['author_chain']
                    or str(mapping['author_residue_id']) != str(identity['author_residue'])
                    or mapping['uniprot_residue'] != identity['canonical_cys']):
                raise ValueError('AF3 review target differs from the current receptor mapping')
        if (review['input_contract'] != contract or review['candidate_artifact_id'] != source['candidate_artifact_id']
                or review['candidate_count'] != len(expected)):
            raise ValueError('Refinement review input contracts differ')
        observed = {(item['input_sdf_index'], item['compound_id']) for item in review['candidates']}
        if observed != expected or len(review['candidates']) != len(expected):
            raise ValueError('Refinement review lost or changed candidate identities')
        for item in review['candidates']:
            rows[(item['input_sdf_index'], item['compound_id'])][method] = item
    projected, structures = [], {}
    for original in raw:
        if original['warhead_class'] != 'acrylamide':
            raise ValueError('Current-study delivery supports its registered acrylamide scope only')
        if original['target_id'] != 'EGFR' or original['site_id'] != 'EGFR_C797':
            raise ValueError('Candidate input target differs from the registered study scope')
        key = original['input_sdf_index'], original['compound_id']
        record = {field: _text(original.get(field)) for field in IDENTITY_FIELDS}
        atom_map = exact_pose_index(original['electrophilic_atom_map'])
        if atom_map < 1:
            raise ValueError('Candidate reaction atom map is absent')
        record['electrophilic_atom'] = 'map:' + str(atom_map)
        # The original table remains copied; this output atom uses author numbering.
        record['nucleophilic_atom'] = f'{identity["author_chain"]}:CYS:{identity["author_residue"]}:SG'
        record['route'] = 'ligand-first'
        for method, score, filename in (('af3', 'af3_mpae', 'model_cif'),
                                         ('covdock', 'covdock_score', 'covdock_pose_file')):
            item = rows[key].get(method)
            record[method + '_status'] = item['engine_status'] if item else 'NOT_RUN'
            record[method + '_failure_reason'] = (item.get('failure_reason') or '') if item else ''
            record[score] = _text(item['score']) if item else ''
            record[filename] = ''
            if item and item['engine_status'] == 'SUCCESS':
                path = item['structure']['path']
                relative = Path(path)
                if relative.is_absolute() or '..' in relative.parts or relative.as_posix() != path:
                    raise ValueError('Refinement structure path is not canonical')
                name = method + '/' + path
                digest = source[method]['output_sha256'].get(path)
                if not digest:
                    raise ValueError('Refinement structure is absent from reviewed output hashes')
                structures[name] = digest
                record[filename] = name
            if method == 'covdock':
                record['covdock_pose_index'] = _text(item['structure']['pose_index']) if item['structure'] else ''
        record.update({key: _text(value) for key, value in rows[key]['covdock']['gate'].items()})
        projected.append(record)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=sorted(projected[0]))
    writer.writeheader(); writer.writerows(projected)
    target = {'attachment_residue': attachment, 'nucleophilic_atom': 'SG',
              'reaction_type': source['reaction_type'], 'receptor_sha256': source['receptor_sha256']}
    return stream.getvalue().encode(), structures, identity, target


def manifest(source, parents, evidence, payload, assets, mapping, identity, target):
    selection = []
    for row in payload['candidates']:
        try:
            _delivered_pose(row, {'target_context': target}, identity)
            result = {'state': 'eligible_for_pose_selection', 'reasons': []}
        except ValueError as error:
            result = {'state': 'ineligible_for_pose_selection', 'reasons': [str(error)]}
        selection.append({'compound_id': row['compound_id'],
                          'input_sdf_index': exact_pose_index(row['input_sdf_index']), **result})
    return {'schema': 'shared-md-candidate-delivery-v1', 'source_profile': PROFILE,
        'status': 'candidate_delivery_frozen_md_selection_pending', 'route': source['route'],
        'task_id': source['task_id'], 'workflow_id': source['workflow_id'],
        'source_evidence': evidence, 'source_assets': mapping, 'parents': parents,
        'target_context': target, 'assets': assets, 'asset_count': len(assets),
        'candidates': payload['candidates'], 'candidate_count': len(payload['candidates']),
        'pose_selection_assessment': selection, 'md_admission': md_not_evaluated(),
        'candidate_selected_for_md': False, 'native_graph_qc_completed': False,
        'calculation_submitted': False}


def freeze(source, table, prepared, context, output, resolver):
    """Local producer only; workflow owns the source and structure resolver."""
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('Current-study delivery output must be new')
    parents = {role: sha256_file(path) for role, path in
               (('candidate_table', table), ('prepared_receptor', prepared), ('receptor_context', context))}
    encoded, structures, identity, target = projection(source, table, prepared, context, parents)
    output.mkdir(parents=True)
    (output/'assets').mkdir()
    copies = {'candidate_table': (table, 'input' + Path(table).suffix),
              'prepared_receptor': (prepared, 'prepared-receptor.json'),
              'receptor_context': (context, 'receptor-context.json')}
    evidence = {}
    for role, (path, name) in copies.items():
        _regular(path, parents[role], role)
        shutil.copyfile(path, output/name)
        _regular(output/name, parents[role], role)
        evidence[role] = {'filename': name, 'sha256': parents[role]}
    (output/'source.json').write_text(json.dumps(source, sort_keys=True, indent=2, allow_nan=False)+'\n')
    (output/'projection.csv').write_bytes(encoded)
    for role, name in (('review_source', 'source.json'), ('projection', 'projection.csv')):
        evidence[role] = {'filename': name, 'sha256': sha256_file(output/name)}
    def resolve(name):
        return _regular(resolver(name), structures[name], 'Reviewed structure')
    payload = build_candidate_results_manifest(output/'projection.csv', route=source['route'],
                                               task_id=source['task_id'], asset_resolver=resolve)
    assets, mapping = {}, {}
    for key, record in payload['assets'].items():
        target_file = output/'assets'/key
        shutil.copyfile(record['path'], target_file)
        _regular(target_file, record['sha256'], 'Frozen structure')
        assets[key] = {'filename': 'assets/'+key, 'sha256': record['sha256'], 'size': record['size']}
    for name in structures:
        path = str(resolve(name).resolve())
        mapping[name] = next(key for key, record in payload['assets'].items() if record['path'] == path)
    value = manifest(source, parents, evidence, payload, assets, mapping, identity, target)
    (output/'candidate-results.json').write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')
    return value


def load(path, value, *, expected_route, expected_task_id):
    folder = path.parent
    evidence = value['source_evidence']
    allowed = {'candidate_table': {'input.csv', 'input.parquet'}, 'prepared_receptor': {'prepared-receptor.json'},
               'receptor_context': {'receptor-context.json'}, 'review_source': {'source.json'}, 'projection': {'projection.csv'}}
    if set(evidence) != set(allowed):
        raise ValueError('Current-study source inventory differs')
    files = {}
    for role, record in evidence.items():
        if set(record) != {'filename', 'sha256'} or record['filename'] not in allowed[role]:
            raise ValueError('Current-study evidence filename differs')
        files[role] = _regular(folder/record['filename'], record['sha256'], role)
    source = json.loads(files['review_source'].read_text())
    if source['route'] != expected_route or source['task_id'] != expected_task_id:
        raise ValueError('Current-study delivery differs from bound route/task')
    parents = {role: evidence[role]['sha256'] for role in ('candidate_table', 'prepared_receptor', 'receptor_context')}
    encoded, structures, identity, target = projection(source, files['candidate_table'],
        files['prepared_receptor'], files['receptor_context'], parents)
    if files['projection'].read_bytes() != encoded or set(value['source_assets']) != set(structures):
        raise ValueError('Candidate projection differs from original reviewed evidence')
    assets, mapping = value['assets'], value['source_assets']
    if set(mapping.values()) != set(assets):
        raise ValueError('Current-study asset mapping differs')
    frozen = {}
    for key, record in assets.items():
        if (Path(key).name != key or key in {'.', '..'} or set(record) != {'filename', 'sha256', 'size'}
                or record['filename'] != 'assets/'+key):
            raise ValueError('Current-study structure filename differs')
        frozen[key] = _regular(folder/record['filename'], record['sha256'], 'Frozen structure')
        if type(record['size']) is not int or frozen[key].stat().st_size != record['size']:
            raise ValueError('Current-study structure size differs')
    for name, digest in structures.items():
        if assets[mapping[name]]['sha256'] != digest:
            raise ValueError('Current-study structure differs from reviewed source')
    payload = build_candidate_results_manifest(files['projection'], route=expected_route,
        task_id=expected_task_id, asset_resolver=lambda name: frozen[mapping[name]])
    replay_assets = {key: {'filename': 'assets/'+key, 'sha256': record['sha256'], 'size': record['size']}
                     for key, record in payload['assets'].items()}
    expected = manifest(source, parents, evidence, payload, replay_assets, mapping, identity, target)
    if value != expected:
        raise ValueError('Current-study delivery differs from replayed evidence')
    return {'directory': folder, 'manifest': value, 'evidence_files': [path, *files.values(), *frozen.values()]}
