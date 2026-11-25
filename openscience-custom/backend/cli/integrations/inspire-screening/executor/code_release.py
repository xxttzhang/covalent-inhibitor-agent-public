"""Content-addressed source releases; no model/data/license copying or execution."""
import argparse
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path

VERSION = 'screening-code-release-v1'
CONTROL = ('worker.py', 'refinement.py', 'engine_interface.py', 'code_release.py')
# New releases include these helpers; older releases remain verifiable without
# them. Consumers requiring a helper must explicitly require its manifest hash.
EXTRA_CONTROL = ('runtime_probe.py', 'refinement_stage.py', 'refinement_readback.py', 'mcp_entry.py')
CONFIG = ('environment/activate_carsidock.sh', 'configs/local.inspire.yaml', 'configs/covindb/EGFR_C797_mvp_v1.yaml')
POLICY = 'science/src/covscreen/docking-engine-policy.json'
PARENT_INDEX = ('science/src/covscreen/data/covpdb_noncovalent_near_cys.tsv',
                'science/src/covscreen/data/covpdb_noncovalent_near_cys.manifest.json')
ROUTE = 'routes/library-first'
LIGAND_ROUTE = 'routes/ligand-first'
DENOVO_ROUTE = 'routes/de-novo'
MAX_FILES = 2000
MAX_BYTES = 64 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def sha(contents):
    return hashlib.sha256(contents).hexdigest()


def allowed(name):
    if not isinstance(name, str) or not name or any(part in ('', '.', '..') for part in name.split('/')) or re.search(r'[\\\x00-\x1f\x7f]', name):
        return False
    route_source = (name.startswith((ROUTE + '/src/covscreen_library_first/', ROUTE + '/scripts/')) and name.endswith('.py'))
    route_config = (name.startswith((ROUTE + '/configs/', ROUTE + '/covindb-configs/')) and name.endswith('.yaml'))
    vendor_python = (name.startswith('science/vendor/InterMol/intermol/') and name.endswith('.py'))
    vendor_pdbfixer = (name.startswith('science/vendor/PDBFixer/pdbfixer/') and
                       Path(name).suffix in {'.py', '.pdb', '.xml'})
    vendor_metadata = (name in {
        'science/vendor/InterMol/LICENSE.md', 'science/vendor/InterMol/PROVENANCE.json',
        'science/vendor/PDBFixer/LICENSE', 'science/vendor/PDBFixer/PROVENANCE.json'})
    ligand_source = (name.startswith((LIGAND_ROUTE + '/src/covscreen_ligand_first/', LIGAND_ROUTE + '/scripts/', LIGAND_ROUTE + '/historical-scripts/')) and name.endswith('.py'))
    ligand_config = (name.startswith((LIGAND_ROUTE + '/configs/', 'science/configs/ligand_first/')) and name.endswith('.yaml'))
    denovo_source = name.startswith(tuple(DENOVO_ROUTE + '/' + part + '/' for part in ('src/covscreen_de_novo', 'scripts', 'historical-scripts', 'runtime', 'evaluation'))) and name.endswith('.py')
    denovo_recipe = name.startswith(DENOVO_ROUTE + '/registry/egfr_c797_v1/') and Path(name).suffix in {'.py', '.json', '.yml', '.yaml', '.pdb', '.cif', '.sdf', '.lock'}
    return (denovo_source or denovo_recipe or route_source or route_config or vendor_python or
            vendor_pdbfixer or vendor_metadata or ligand_source or ligand_config or name in CONTROL + EXTRA_CONTROL or name in {'science/' + item for item in CONFIG} or
            name in PARENT_INDEX or name == POLICY or (name.endswith('.py') and name.startswith(('science/scripts/', 'science/src/covscreen/'))))


def manifest(files):
    if not isinstance(files, dict) or not files or len(files) > MAX_FILES or any(not allowed(name) for name in files):
        raise ValueError('Invalid source release inventory')
    required = {*CONTROL, *('science/' + item for item in CONFIG),
                'science/scripts/run_af3_registered_batch.py', 'science/scripts/run_covdock_lo_registered.py',
                'science/src/covscreen/__init__.py', 'science/src/covscreen/refinement_input.py'}
    if not required.issubset(files):
        raise ValueError('Source release lacks required entry/guard/config files')
    if any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value) for value in files.values()):
        raise ValueError('Invalid source file checksum')
    return {'version': VERSION, 'files': dict(sorted(files.items()))}


def describe(contents):
    value = json.loads(contents)
    if not isinstance(value, dict) or set(value) != {'version', 'files'} or value['version'] != VERSION:
        raise ValueError('Invalid source release manifest')
    parsed = manifest(value['files'])
    if canonical(parsed) + b'\n' != contents:
        raise ValueError('Noncanonical source release manifest')
    return parsed, sha(canonical(parsed)), sha(contents)


def archive_files(archive, expected):
    if not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('Explicit code release ID required')
    files = {}
    total = 0
    with tarfile.open(archive, 'r:gz') as stream:
        for item in stream:
            if not item.isfile() or item.name in files or (item.name != 'manifest.json' and not allowed(item.name)):
                raise ValueError('Archive includes duplicate, nonregular or unapproved paths')
            total += item.size
            if item.size < 0 or total > MAX_BYTES or len(files) > MAX_FILES:
                raise ValueError('Source archive exceeds bounded size')
            files[item.name] = stream.extractfile(item).read()
    value, identity, checksum = describe(files.pop('manifest.json'))
    if identity != expected or value['files'] != {name: sha(data) for name, data in files.items()}:
        raise ValueError('Source archive identity/content differs')
    return files, value, identity, checksum


def build(project, output, runtime_config=None):
    project = Path(project).resolve(strict=True)
    executor = Path(__file__).resolve().parent
    sources = {name: executor / name for name in CONTROL + EXTRA_CONTROL}
    for folder in ('scripts', 'src/covscreen'):
        for item in (project / folder).rglob('*.py'):
            sources['science/' + item.relative_to(project).as_posix()] = item
    for vendor in ('InterMol', 'PDBFixer'):
        for item in (project / 'vendor' / vendor).rglob('*'):
            name = 'science/' + item.relative_to(project).as_posix()
            if item.is_file() and not item.is_symlink() and allowed(name):
                sources[name] = item
    sources.update({'science/' + name: project / name for name in CONFIG})
    # A Python import is not self-contained without its exact policy data.
    # Older immutable manifests remain verifiable; every new build includes it.
    sources[POLICY] = project / 'src/covscreen/docking-engine-policy.json'
    if ((project / 'src/covscreen/ligand_parent_index.py').is_file()
        and all((project / name.removeprefix('science/')).is_file() for name in PARENT_INDEX)):
        for name in PARENT_INDEX:
            sources[name] = project / name.removeprefix('science/')
    # A machine configuration is an explicit external build input. Its bytes
    # remain covered by the release identity and by mcp_entry's data-root check.
    runtime = Path(runtime_config).absolute() if runtime_config else None
    if runtime is not None:
        if runtime.is_symlink() or not runtime.is_file():
            raise ValueError('Runtime configuration must be a regular file')
        runtime = runtime.resolve(strict=True)
        sources['science/configs/local.inspire.yaml'] = runtime
    route = project.parent / ROUTE
    if route.is_dir():
        for folder, suffix in [('src/covscreen_library_first', '*.py'), ('scripts', '*.py'), ('configs', '*.yaml'), ('covindb-configs', '*.yaml')]:
            for item in (route / folder).rglob(suffix):
                sources[ROUTE + '/' + item.relative_to(route).as_posix()] = item
        # Compatibility config location in releases is materialized from the
        # canonical source, not from an arbitrary followed symlink.
        sources['science/configs/covindb/EGFR_C797_mvp_v1.yaml'] = route / 'covindb-configs/EGFR_C797_mvp_v1.yaml'
    ligand = project.parent / LIGAND_ROUTE
    if ligand.is_dir():
        for folder, suffix in [('src/covscreen_ligand_first', '*.py'), ('scripts', '*.py'), ('historical-scripts', '*.py'), ('configs', '*.yaml')]:
            for item in (ligand / folder).rglob(suffix):
                sources[LIGAND_ROUTE + '/' + item.relative_to(ligand).as_posix()] = item
        # Frozen configuration references retain their legacy address; both
        # release paths are materialized from the single maintained source.
        for item in (ligand / 'configs').glob('*.yaml'):
            sources['science/configs/ligand_first/' + item.name] = item
    denovo = project.parent / DENOVO_ROUTE
    for folder in ('src/covscreen_de_novo', 'scripts', 'historical-scripts', 'runtime', 'evaluation', 'registry/egfr_c797_v1'):
        for item in (denovo / folder).rglob('*'):
            name = DENOVO_ROUTE + '/' + item.relative_to(denovo).as_posix()
            if item.is_file() and not item.is_symlink() and allowed(name):
                sources[name] = item
    files = {}
    for name, source in sorted(sources.items()):
        if source.is_symlink() or not source.is_file():
            raise ValueError('Source must be a regular file: ' + name)
        base = runtime.parent if runtime is not None and source == runtime else executor if name in CONTROL + EXTRA_CONTROL else (denovo if source.is_relative_to(denovo) else ligand if source.is_relative_to(ligand) else route if source.is_relative_to(route) else project)
        source.resolve(strict=True).relative_to(base)
        files[name] = source.read_bytes()
    if sum(map(len, files.values())) > MAX_BYTES:
        raise ValueError('Source release exceeds bounded size')
    value = manifest({name: sha(data) for name, data in files.items()})
    identity = sha(canonical(value))
    files['manifest.json'] = canonical(value) + b'\n'
    output = Path(output).resolve(strict=True)
    destination = output / (identity + '.tar.gz')
    if not destination.exists():
        # Exclusive output; an interrupted partial file is retained, not silently replaced.
        with destination.open('xb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as zipped:
                with tarfile.open(fileobj=zipped, mode='w') as archive:
                    for name, data in sorted(files.items()):
                        item = tarfile.TarInfo(name)
                        item.size, item.mode, item.mtime = len(data), 0o444, 0
                        archive.addfile(item, io.BytesIO(data))
            raw.flush()
            os.fsync(raw.fileno())
    archive_files(destination, identity)
    return {'release_id': identity, 'manifest_sha256': sha(files['manifest.json']),
            'archive': str(destination), 'archive_sha256': sha(destination.read_bytes()), 'file_count': len(value['files'])}


def verify(root, expected, expected_manifest):
    if any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value) for value in (expected, expected_manifest)):
        raise ValueError('Explicit release and manifest digests required')
    root = Path(root)
    if root.is_symlink() or root.name != expected:
        raise ValueError('Code release root identity differs')
    root = root.resolve(strict=True)
    entry = root / 'manifest.json'
    if entry.is_symlink():
        raise ValueError('Release manifest cannot be a symbolic link')
    value, identity, checksum = describe(entry.read_bytes())
    if identity != expected or checksum != expected_manifest:
        raise ValueError('Frozen release manifest changed')
    found = {}
    total = 0
    for directory, directories, filenames in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(directory) / name).is_symlink():
                raise ValueError('Source directory cannot be a symbolic link')
        for name in filenames:
            file = Path(directory) / name
            relative = file.relative_to(root).as_posix()
            if file.is_symlink() or not file.is_file():
                raise ValueError('Code release contains nonregular files')
            if relative == 'manifest.json':
                continue
            if relative not in value['files']:
                raise ValueError('Unrecorded executable/source file in release')
            total += file.stat().st_size
            if total > MAX_BYTES:
                raise ValueError('Released source exceeds bounded size')
            found[relative] = sha(file.read_bytes())
    if found != value['files']:
        raise ValueError('Released source missing or changed')
    return {'release_id': identity, 'manifest_sha256': checksum, 'root': str(root), 'files': found}


def install(archive, releases, expected, expected_manifest):
    files, value, identity, checksum = archive_files(archive, expected)
    if checksum != expected_manifest:
        raise ValueError('Archive manifest differs from installation request')
    releases = Path(releases).resolve(strict=True)
    destination = releases / identity
    with (releases / '.install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists() or destination.is_symlink():
            return verify(destination, identity, checksum)  # Never overwrite an existing version.
        stage = Path(tempfile.mkdtemp(prefix='.' + identity + '.', dir=releases))
        for name, data in {**files, 'manifest.json': canonical(value) + b'\n'}.items():
            file = stage / name
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            file.chmod(0o444)
        stage.rename(destination)
        return verify(destination, identity, checksum)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='action', required=True)
    build_parser = commands.add_parser('build')
    build_parser.add_argument('--project', required=True)
    build_parser.add_argument('--output', required=True)
    build_parser.add_argument('--runtime-config', help='Explicit external local.inspire.yaml; included in release identity')
    install_parser = commands.add_parser('install')
    install_parser.add_argument('--archive', required=True)
    install_parser.add_argument('--releases', required=True)
    install_parser.add_argument('--release-id', required=True)
    install_parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    result = build(args.project, args.output, args.runtime_config) if args.action == 'build' else install(
        args.archive, args.releases, args.release_id, args.manifest_sha256)
    print(json.dumps({key: value for key, value in result.items() if key != 'files'}, sort_keys=True))
