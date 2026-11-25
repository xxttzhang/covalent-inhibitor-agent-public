"""Enforce verified parent artifacts at the registered execution boundary."""
from pathlib import Path
import json

from .jobs import ArtifactRegistry
from .result_verification import verify_job


def require_prescreen_parent(root, request):
    from .branch_guard import require_unquarantined
    require_unquarantined(root, request)
    if request.route == 'library-first' and request.engine == 'rtmscore':
        if not request.parent_run_id:
            raise ValueError('RTMScore requires a verified CarsiDock parent_run_id')
        registry = ArtifactRegistry(root)
        spec = registry.load_spec(request.parent_run_id)
        if spec.engine != 'carsidock-cov' or spec.route != 'library-first':
            raise ValueError('RTMScore parent is not Library-first CarsiDock')
        verification = verify_job(root, request.parent_run_id)
        if not verification['verified']:
            raise ValueError('RTMScore parent verification failed')
        require_prescreen_parent(root, spec)
        expected = (Path(root) / spec.output_directory / 'carsidock_scores.parquet').resolve()
        if (Path(root) / request.parameters.get('carsidock_scores', '')).resolve() != expected:
            raise ValueError('RTMScore input differs from verified parent scores')
        for key in ('pdb', 'reference_ligand'):
            if (Path(root) / request.parameters.get(key, '')).resolve() != (Path(root) / spec.parameters[key]).resolve():
                raise ValueError('RTMScore receptor/reference differs from parent')
        return
    if request.route != 'library-first' or request.engine != 'carsidock-cov':
        return
    registry = ArtifactRegistry(root)
    parent = request.parent_run_id
    seen = set()
    while parent:
        if parent in seen or len(seen) >= 100:
            raise ValueError('Cyclic or oversized docking ancestry')
        seen.add(parent)
        spec = registry.load_spec(parent)
        if spec.engine == 'covindb-prescreen':
            verified = verify_job(root, parent)
            if not verified['verified']:
                raise ValueError('Prescreen parent failed verification: ' + '; '.join(verified['errors']))
            output = Path(spec.output_directory)
            if not output.is_absolute():
                output = Path(root) / output
            for key, filename in [('ligand_table', 'stage2_candidates.parquet'), ('ligand_sdf', 'stage2_candidates.sdf')]:
                value = request.parameters.get(key)
                if not value or (Path(root) / value).resolve() != (output / filename).resolve():
                    raise ValueError('Docking input differs from verified prescreen parent: ' + key)
            if spec.parameters.get('pilot_manifest'):
                binding_name = request.parameters.get('docking_binding')
                if not binding_name:
                    raise ValueError('Site-bound pilot requires a frozen docking_binding')
                binding_path = (Path(root) / binding_name).resolve()
                binding_path.relative_to(Path(root).resolve())
                binding = json.loads(binding_path.read_text())
                pilot = json.loads((Path(root) / spec.parameters['pilot_manifest']).read_text())
                if (binding.get('prescreen_run_id') != parent or binding.get('selection') != pilot['selection']
                        or binding.get('structure') != pilot['structure'] or binding.get('site') != pilot['site']
                        or binding.get('prescreen_spec_sha256') != verified['spec_sha256']
                        or binding.get('prescreen_result_sha256') != verified['result_sha256']):
                    raise ValueError('Docking binding differs from verified pilot')
                from .library_site_handoff import verified_selection
                _, selection, _ = verified_selection(Path(root), binding['selection'])
                if binding['structure'] != selection['structure'] or binding['site'] not in selection['selected_sites']:
                    raise ValueError('Docking binding differs from frozen sites')
                from .docking_reference import validate_reference
                asset = {**binding['reference'],
                         'pdb': str(Path(root) / request.parameters['pdb']),
                         'reference_ligand': str(Path(root) / request.parameters['reference_ligand'])}
                reference = validate_reference(asset, binding['structure'], binding['site'])
                if request.parameters.get('covres') != reference['covres']:
                    raise ValueError('Docking residue differs from verified author numbering')
            return
        if spec.engine != 'carsidock-cov' or spec.route != 'library-first':
            raise ValueError('Docking ancestry must lead to the registered prescreen')
        parent = spec.parent_run_id
    raise ValueError('Library-first docking requires a verified prescreen parent_run_id')
