"""Freeze the bounded vendor reader separately from scoring or authorization."""
from pathlib import Path

from .covdock_receptor import runtime_paths
from .jobs import sha256_file
from .layout import science_script


class CovDockReadbackUnavailable(ValueError):
    """Readback cannot run here; this is not negative molecular evidence."""


def sources():
    from . import refinement_output, covdock_gate, covdock_receptor, refinement_quality, ligand_covdock_evidence
    return {module.__name__: sha256_file(Path(module.__file__))
            for module in (refinement_output, covdock_gate, covdock_receptor, refinement_quality, ligand_covdock_evidence)} | {
                __name__: sha256_file(Path(__file__))}


def freeze(configuration, attachment):
    paths = runtime_paths(configuration)
    script = science_script('inspect_covdock_output.py').resolve(strict=True)
    runtime = {'name': 'covdock', 'attachment_residue': attachment,
        'schrodinger_root': str(paths['root'].resolve(strict=True)),
        'license_file': str(paths['license_file'].resolve(strict=True)),
        'runtime_lib_dir': str(paths['runtime_lib_dir'].resolve(strict=True)) if paths['runtime_lib_dir'] else None}
    assets = [script, Path(runtime['schrodinger_root']) / 'run', Path(runtime['license_file'])]
    return {'schema': 'covdock-readback-runtime-v1', 'runtime': runtime, 'script': str(script),
        'assets': {str(path): sha256_file(path) for path in assets}, 'sources': sources(),
        'scope': 'bounded_read_only_structure_inspection', 'scientific_acceptance': False}


def validate(binding, attachment, *, allow_reassessment=False):
    from .refinement_output import Files
    if not binding:
        raise CovDockReadbackUnavailable('This plan has no frozen CovDock readback runtime')
    current_script = science_script('inspect_covdock_output.py').resolve(strict=True)
    current_sources = sources()
    if (binding.get('schema') != 'covdock-readback-runtime-v1'
            or binding['runtime'].get('attachment_residue') != attachment):
        raise ValueError('CovDock reader implementation or target differs from the frozen plan')
    files = Files('/')
    if allow_reassessment:
        # The original runtime remains frozen. Reassessment may update the
        # Python evaluator, but must retain and verify every original source
        # and use the byte-identical bounded vendor inspection script.
        frozen_script = Path(binding['script'])
        if (not frozen_script.is_absolute() or frozen_script.name != current_script.name
                or frozen_script.parent.name != 'scripts'
                or set(binding.get('sources', {})) != set(current_sources)
                or binding['assets'].get(str(frozen_script)) != sha256_file(current_script)):
            raise ValueError('CovDock frozen reader cannot support this reassessment')
        for module, digest in binding['sources'].items():
            original = frozen_script.parent.parent / 'src' / (module.replace('.', '/') + '.py')
            files.read(str(original), digest)
    elif (binding.get('sources') != current_sources
            or binding.get('script') != str(current_script)):
        raise ValueError('CovDock reader implementation or target differs from the frozen plan')
    required = {binding['script'], str(Path(binding['runtime']['schrodinger_root']) / 'run'),
                binding['runtime']['license_file']}
    if set(binding['assets']) != required:
        raise ValueError('CovDock readback asset inventory differs')
    for name in required:
        if not Path(name).is_file():
            raise CovDockReadbackUnavailable('Frozen CovDock readback asset is unavailable')
        files.read(name, binding['assets'][name])
    return binding
