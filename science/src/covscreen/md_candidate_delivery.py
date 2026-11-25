"""Shared frozen delivery for evaluated candidates; discovery provenance stays explicit."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import re

from covscreen.candidate_delivery import build_candidate_results_manifest, _read_table, _text
from covscreen.jobs import sha256_file

def _regular(path, digest, label):
    path = Path(path)
    if (any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file()
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or sha256_file(path) != digest):
        raise ValueError(label + " checksum mismatch")
    return path



def freeze_md_candidate_delivery(candidate_table, candidate_table_sha256,
                                 covdock_manifest, covdock_manifest_sha256,
                                 task_id, output, *, route, legacy_library=False):
    if route not in {"library-first", "ligand-first", "de-novo"} or legacy_library and route != "library-first":
        raise ValueError("Unsupported candidate discovery route")
    table = _regular(candidate_table, candidate_table_sha256, "Candidate table")
    manifest_path = _regular(covdock_manifest, covdock_manifest_sha256, "CovDock manifest")
    covdock = json.loads(manifest_path.read_text())
    if not legacy_library:
        frame = _read_table(table)
        if "route" not in frame or any(_text(value) != route for value in frame["route"]):
            raise ValueError("Shared MD delivery requires explicit consistent candidate route records")
    gates = covdock.get("gate_quality_gates")
    if (covdock.get("status") != "completed"
            or not isinstance(gates, dict) or not gates
            or not all(value is True for value in gates.values())
            or not covdock.get("pose_files")
            or not covdock.get("attachment_residue")
            or covdock.get("nucleophilic_atom") != "SG"
            or not covdock.get("reaction_type")):
        raise ValueError("CovDock parent is not eligible for MD candidate delivery")
    payload = build_candidate_results_manifest(table, route=route, task_id=task_id)
    pose_files = {str(Path(path).resolve()) for path in covdock["pose_files"]}
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Candidate delivery output must be new")
    (output / "assets").mkdir(parents=True)
    frozen_assets = {}
    for key, record in sorted(payload["assets"].items()):
        source = Path(record["path"]).resolve()
        if key.startswith("covdock_") and str(source) not in pose_files:
            raise ValueError("Candidate CovDock asset is absent from the registered run manifest")
        target = output / "assets" / key
        shutil.copyfile(source, target)
        if sha256_file(target) != record["sha256"]:
            raise ValueError("Copied candidate asset checksum mismatch")
        frozen_assets[key] = {"filename": f"assets/{key}", "sha256": record["sha256"],
            "size": target.stat().st_size}
    result = {"schema":"library-first-md-candidate-delivery-v1" if legacy_library else "shared-md-candidate-delivery-v1",
        "status":"candidate_delivery_frozen_md_selection_pending",
        "route":route,"task_id":task_id,
        "target_context":{"attachment_residue":covdock["attachment_residue"],
            "nucleophilic_atom":"SG","reaction_type":covdock["reaction_type"],
            "receptor_sha256":covdock.get("input_receptor_sha256")},
        "parents":{"candidate_table_sha256":candidate_table_sha256,
            "covdock_manifest_sha256":covdock_manifest_sha256},
        "assets":frozen_assets,"asset_count":len(frozen_assets),
        "candidates":payload["candidates"],
        "candidate_count":len(payload["candidates"]),"candidate_selected_for_md":False,
        "native_graph_qc_completed":False,"calculation_submitted":False}
    if not legacy_library:
        sources = {}
        for role, source, digest in (("candidate_table", table, candidate_table_sha256),
                                     ("covdock_manifest", manifest_path, covdock_manifest_sha256)):
            filename = role + (source.suffix.lower() if role == "candidate_table" else ".json")
            shutil.copyfile(source, output / filename)
            if sha256_file(output / filename) != digest:
                raise ValueError("Copied source evidence checksum mismatch")
            sources[role] = {"filename": filename, "sha256": digest}
        result["source_assets"] = {record["path"]: key for key, record in payload["assets"].items()}
        result["source_evidence"] = sources
        result["source_profile"] = "evaluated-af3-covdock-candidate-table-v1"
    path=output/"candidate-results.json"
    path.write_text(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False)+"\n")
    return result


def load_shared_candidate_delivery(manifest_path, manifest_sha256, *, expected_route, expected_task_id):
    """Replay copied producer evidence; caller must bind route/task to its approved workflow."""
    path = _regular(manifest_path, manifest_sha256, "Shared candidate delivery")
    value = json.loads(path.read_text())
    if isinstance(value, dict) and value.get('source_profile') == 'current-study-refinement-reviews-v1':
        from .md_review_delivery import load
        return load(path, value, expected_route=expected_route, expected_task_id=expected_task_id)
    if (not isinstance(value, dict) or value.get('schema') != 'shared-md-candidate-delivery-v1'
            or value.get('source_profile') != 'evaluated-af3-covdock-candidate-table-v1'
            or value.get('route') != expected_route or value.get('task_id') != expected_task_id
            or value.get('status') != 'candidate_delivery_frozen_md_selection_pending'
            or any(value.get(key) is not False for key in
                   ('candidate_selected_for_md', 'native_graph_qc_completed', 'calculation_submitted'))):
        raise ValueError('Shared delivery identity or state is inconsistent')
    folder = path.parent
    sources = value.get('source_evidence', {})
    if not isinstance(sources, dict) or set(sources) != {'candidate_table', 'covdock_manifest'}:
        raise ValueError('Shared delivery source inventory is incomplete')
    source_paths = {}
    for role, record in sources.items():
        allowed = {'candidate_table.csv', 'candidate_table.parquet'} if role == 'candidate_table' else {'covdock_manifest.json'}
        if not isinstance(record, dict) or set(record) != {'filename', 'sha256'} or record['filename'] not in allowed:
            raise ValueError('Shared delivery source filename is invalid')
        if record['sha256'] != value.get('parents', {}).get(role + '_sha256'):
            raise ValueError('Shared delivery source differs from parent hash')
        source_paths[role] = _regular(folder / record['filename'], record['sha256'], role)
    assets = value.get('assets')
    mapping = value.get('source_assets')
    if (not isinstance(assets, dict) or not isinstance(mapping, dict)
            or set(mapping.values()) != set(assets) or len(mapping) != len(assets)):
        raise ValueError('Shared delivery source asset mapping is inconsistent')
    frozen = {}
    for key, record in assets.items():
        if (not isinstance(key, str) or Path(key).name != key or key in {'.', '..'}
                or not isinstance(record, dict) or set(record) != {'filename', 'sha256', 'size'}
                or record['filename'] != 'assets/' + key):
            raise ValueError('Shared delivery asset filename is invalid')
        frozen[key] = _regular(folder / record['filename'], record['sha256'], 'Shared structure')
        if type(record['size']) is not int or frozen[key].stat().st_size != record['size']:
            raise ValueError('Shared structure size differs from registered copy')
    frame = _read_table(source_paths['candidate_table'])
    if 'route' not in frame or any(_text(item) != expected_route for item in frame['route']):
        raise ValueError('Copied candidate source route is inconsistent')
    def resolve(original):
        key = mapping.get(str(Path(original).resolve()))
        if key not in frozen:
            raise ValueError('Candidate source structure lacks its frozen mapping')
        return frozen[key]
    replay = build_candidate_results_manifest(source_paths['candidate_table'], route=expected_route,
        task_id=expected_task_id, asset_resolver=resolve)
    if (replay['candidates'] != value.get('candidates') or set(replay['assets']) != set(assets)
            or value.get('candidate_count') != len(replay['candidates']) or value.get('asset_count') != len(assets)
            or any(replay['assets'][key]['sha256'] != record['sha256'] or replay['assets'][key]['size'] != record['size']
                   for key, record in assets.items())):
        raise ValueError('Shared delivery candidates differ from replayed source evidence')
    native = json.loads(source_paths['covdock_manifest'].read_text())
    gates = native.get('gate_quality_gates')
    if (native.get('status') != 'completed' or not isinstance(gates, dict) or not gates
            or not all(item is True for item in gates.values()) or native.get('nucleophilic_atom') != 'SG'
            or not native.get('attachment_residue') or not native.get('reaction_type')):
        raise ValueError('Copied CovDock source is not eligible')
    registered = {str(Path(item).resolve()) for item in native.get('pose_files', [])}
    if any(original not in registered for original, key in mapping.items() if key.startswith('covdock_')):
        raise ValueError('Frozen CovDock asset is absent from the copied producer manifest')
    context = {'attachment_residue': native['attachment_residue'], 'nucleophilic_atom': 'SG',
        'reaction_type': native['reaction_type'], 'receptor_sha256': native.get('input_receptor_sha256')}
    if value.get('target_context') != context:
        raise ValueError('Shared delivery target differs from the copied producer manifest')
    return {'directory': folder, 'manifest': value,
        'evidence_files': [path, *source_paths.values(), *frozen.values()]}
