"""Build the next bounded route plan from verified pilot outputs and registered assets."""
import hashlib
import json
from pathlib import Path

from pydantic import Field

from .site_contracts import StrictInput
from .jobs import sha256_file
from .library_site_handoff import verified_selection
from .result_verification import verify_job
from .docking_reference import validate_reference
from .structure_coverage import observed_coverage


class PilotDockingInput(StrictInput):
    prescreen_run_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$')


def prepare_docking(root: Path, arguments: dict) -> dict:
    args = PilotDockingInput.model_validate(arguments)
    root = Path(root).resolve()
    from .execution import JobManager
    manager = JobManager(root)
    verification = verify_job(root, args.prescreen_run_id)
    if not verification['verified']:
        return {**verification, 'calculation_submitted': False}
    spec = manager.registry.load_spec(args.prescreen_run_id)
    if spec.engine != 'covindb-prescreen':
        raise ValueError('A verified prescreen job is required')
    name = spec.parameters.get('pilot_manifest')
    if not name:
        raise ValueError('Prescreen is not bound to a frozen site pilot')
    path = (root / name).resolve()
    path.relative_to(root)
    if spec.input_sha256.get(name) != sha256_file(path):
        raise ValueError('Pilot manifest differs from approved prescreen input')
    pilot = json.loads(path.read_text())
    _, selection, _ = verified_selection(root, pilot['selection'])
    if pilot['target'] != selection['target'] or pilot['structure'] != selection['structure'] or pilot['site'] not in selection['selected_sites']:
        raise ValueError('Pilot target, structure or site differs from frozen selection')
    registry_path = root / 'configs/library_docking_references.json'
    if registry_path.is_symlink():
        raise ValueError('Reference registry must not be a symlink')
    registered = json.loads(registry_path.read_text()) if registry_path.is_file() else {'schema': 'library-docking-references-v1', 'references': []}
    if registered.get('schema') != 'library-docking-references-v1':
        raise ValueError('Unsupported reference registry schema')
    assets = registered['references']
    matches = [asset for asset in assets if asset.get('pdb_sha256') == selection['structure']['input_sha256']]
    if len(matches) != 1:
        return {'status': 'BLOCKED', 'reason': 'Exactly one registered experimental reference for the frozen receptor is required',
                'required_asset_fields': ['pdb', 'pdb_sha256', 'reference_ligand', 'reference_sha256', 'component_id', 'component_chain', 'component_residue_id'],
                'prescreen_run_id': args.prescreen_run_id, 'calculation_submitted': False}
    reference = validate_reference(matches[0], selection['structure'], pilot['site'])
    coverage = observed_coverage(Path(reference['pdb']), selection['structure'])
    contract = {'schema': 'pilot-docking-binding-v1', 'prescreen_run_id': args.prescreen_run_id,
                'prescreen_spec_sha256': verification['spec_sha256'], 'prescreen_result_sha256': verification['result_sha256'],
                'selection': pilot['selection'], 'structure': pilot['structure'], 'site': pilot['site'],
                'reference': reference, 'terminal_stage': 'rtmscore'}
    raw = (json.dumps(contract, sort_keys=True, separators=(',', ':')) + '\n').encode()
    identity = hashlib.sha256(raw).hexdigest()
    directory = root / 'state' / 'pilot_docking' / identity
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in [('binding.json', raw), ('receptor.pdb', Path(reference['pdb']).read_bytes()),
                              ('reference.sdf', Path(reference['reference_ligand']).read_bytes())]:
        destination = directory / filename
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != content:
                raise ValueError('Frozen docking input changed')
        else:
            with destination.open('xb') as handle:
                handle.write(content)
    from .library_chain import LibraryFirstChainConfig
    config = LibraryFirstChainConfig(campaign_id='pilot-docking-' + identity[:24], project_id=spec.project_id,
        target_config=spec.parameters['target_config'], terminal_stage='rtmscore',
        pdb=str((directory / 'receptor.pdb').relative_to(root)),
        reference_ligand=str((directory / 'reference.sdf').relative_to(root)),
        docking_binding=str((directory / 'binding.json').relative_to(root)),
        attachment_residue=f"{pilot['site']['author_chain']}:{pilot['site']['author_residue_id']}", covres=reference['covres'],
        seed_run_ids={'prescreen': args.prescreen_run_id}, seed=spec.parameters['seed'],
        docking_max_runtime_minutes=60, scoring_max_runtime_minutes=30)
    config_path = directory / 'chain.json'
    content = config.model_dump_json(indent=2)
    if config_path.exists() and config_path.read_text() != content:
        raise ValueError('Frozen docking route configuration changed')
    if not config_path.exists():
        config_path.write_text(content)
    from .tool_api import library_first_chain_tool
    response = library_first_chain_tool(root, str(config_path), execution_mode='plan', submit=False)
    request = response.get('next_request')
    if request:
        from .jobs import JobSubmissionRequest
        response['execution_plan'] = manager.build_spec(JobSubmissionRequest.model_validate(request)).model_dump(mode='json')
    response.update(binding_sha256=sha256_file(directory / 'binding.json'),
                    structure_coverage=coverage,
                    existing_results_reused=[args.prescreen_run_id], calculation_submitted=False,
                    other_frozen_sites=[s for s in selection['selected_sites'] if s != pilot['site']],
                    approval_required=True, next_tool='covalent_submit_job',
                    instruction='Use the exact next_request to request native approval; do not self-approve or treat this plan as execution.')
    return response
