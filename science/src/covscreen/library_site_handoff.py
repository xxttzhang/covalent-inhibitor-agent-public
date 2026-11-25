"""Read-only, hash-bound bridge from shared site selection to route planning.

A site selection is not a LibraryFirstRequest. Never invent a path, fabricate
project evidence, or register a campaign merely because a site was selected.
"""
from pathlib import Path
import json
from pydantic import Field

from .site_contracts import StrictInput, Sha
from .covalent_site_discovery import (
    SELECTION_SCHEMA, _load_evaluation, _completed_prediction,
)
from .jobs import sha256_file


class LibrarySitePlanInput(StrictInput):
    site_selection_artifact_id: str = Field(pattern=r'^site-selection-[a-f0-9]{32}$')
    site_selection_manifest_sha256: Sha


def _artifact(root, relative, checksum):
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing or symlinked site-selection manifest')
    path.resolve().relative_to(root)
    if sha256_file(path) != checksum:
        raise ValueError('Site-selection manifest checksum mismatch')
    return json.loads(path.read_text())


def verified_selection(root: Path, arguments: dict) -> tuple:
    args = LibrarySitePlanInput.model_validate(arguments)
    root = root.resolve()
    selection = _artifact(root, Path('state/covalent_site_selection') /
        args.site_selection_artifact_id.removeprefix('site-selection-') / 'site-selection.json',
        args.site_selection_manifest_sha256)
    if (selection.get('schema') != SELECTION_SCHEMA or
            selection.get('status') != 'SITES_FROZEN_ROUTE_INPUT_READY'):
        raise ValueError('Completed site selection required')
    if not any(h.get('route') == 'library-first' for h in selection['route_handoffs']):
        raise ValueError('Selection does not authorize the Library-first route')
    parent = selection['parents']
    evaluation, _ = _load_evaluation(root, parent['evaluation_artifact_id'],
                                    parent['evaluation_manifest_sha256'])
    parent = evaluation['parents']
    prediction, _ = _completed_prediction(root, parent['prediction_run_id'],
                                          parent['prediction_manifest_sha256'])
    for field in ('target', 'structure'):
        if selection[field] != evaluation[field] or selection[field] != prediction[field]:
            raise ValueError('Selection parent identity mismatch')
    eligible = {r['canonical_position']: r for r in evaluation['candidates']
                if r['eligible_for_exploratory_selection']}
    seen = set()
    for site in selection['selected_sites']:
        position = site['canonical_position']
        row = eligible.get(position)
        if position in seen or row is None or any(row.get(k) != v for k, v in site.items()):
            raise ValueError('Selected site differs from verified evaluation')
        seen.add(position)
    if not seen:
        raise ValueError('Empty site selection')

    return args, selection, parent


def plan_from_sites(root: Path, arguments: dict) -> dict:
    root = root.resolve()
    args, selection, parent = verified_selection(root, arguments)

    # Only discover formal request documents under registered projects. Files
    # elsewhere (including the selection JSON itself) are not route requests.
    from .research import LibraryFirstRequest, TargetPassport
    from .tool_api import load_project_state, plan_library_cascade_tool, resolve_within
    requests = []
    for path in sorted((root / 'projects').glob('*/plans/*library*request*.json')):
        if path.is_symlink():
            raise ValueError('Symlinked Library-first request')
        path.resolve().relative_to(root)
        request = LibraryFirstRequest.model_validate(json.loads(path.read_text()))
        project, passport, _, _ = load_project_state(root, request.project_id)
        path.resolve().relative_to(project)
        if (passport.symbol, passport.uniprot_id) != (
                selection['target']['gene'], selection['target']['uniprot']):
            continue
        requests.append((path, project, passport, request))

    sites = []
    for site in selection['selected_sites']:
        matches = []
        for path, project, passport, request in requests:
            # The engine uses AUTHOR numbering, never canonical numbering by assumption.
            if (request.chain_id, str(request.residue_number), request.reactive_atom,
                    request.residue_name) != (site['author_chain'], site['author_residue_id'],
                                              site['reactive_atom'], 'CYS'):
                continue
            receptor = resolve_within(project, request.receptor_path)
            if not receptor.is_file() or sha256_file(receptor) != selection['structure'].get('input_sha256'):
                continue
            for relative in (request.target_passport_path, request.ligand_table_path, request.output_dir):
                resolve_within(project, relative)
            bound_passport = TargetPassport.model_validate(json.loads(
                resolve_within(project, request.target_passport_path).read_text()))
            if bound_passport != passport:
                raise ValueError("Request passport differs from registered project passport")
            matches.append((path, request))
        if len(matches) > 1:
            raise ValueError('Ambiguous registered Library-first requests for ' + site['site'])
        if not matches:
            sites.append({'site': site, 'state': 'BLOCKED_MISSING_ROUTE_INPUTS',
                'blockers': ['No registered LibraryFirstRequest bound to this target, author residue and exact receptor hash'],
                'required_inputs': ['registered project passport, assessment and route plan',
                    'exact receptor with author residue numbering', 'compound library table with provenance',
                    'reaction type, enabled engines and explicit resource budget'],
                'request_search_pattern': 'projects/*/plans/*library*request*.json'})
            continue
        path, request = matches[0]
        planned = plan_library_cascade_tool(root, str(path), require_inputs=True)
        if planned['status'] != 'PASS':
            return {'status': 'FAIL', 'site': site, 'validation': planned,
                    'calculation_submitted': False}
        sites.append({'site': site, 'state': ('BLOCKED_CASCADE' if planned['plan']['decision'] in ('BLOCKED', 'STOP') else 'PLAN_GENERATED'),
            'request_path': str(path.relative_to(root)), 'request_sha256': sha256_file(path),
            'resource_budget': request.resource_budget.model_dump(mode='json'), 'cascade': planned})
    return {'status': 'BLOCKED' if any(s['state'].startswith('BLOCKED') for s in sites) else 'PLANNED',
        'input': args.model_dump(), 'target': selection['target'], 'structure': selection['structure'],
        'prediction_source': parent, 'sites': sites,
        'next_action': 'If no registered request exists, call covalent_library_first_prepare_pilot with this selection ID/hash and a selected canonical_position to build a fresh bounded request. Never pass a site-selection JSON as request_path.',
        'approval_requirement': 'A fresh exact execution plan and native approval are required before each actual computation.',
        'calculation_submitted': False, 'campaign_registered': False,
        'scope': 'candidate list and scores only; no parameterization or MD'}
