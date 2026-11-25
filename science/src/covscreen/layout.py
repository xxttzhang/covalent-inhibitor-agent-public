"""Locate canonical route code in a checkout or an immutable source release."""
from pathlib import Path
import sys


def resolve_route_config(project: Path, value: str | Path) -> Path:
    root = Path(project).resolve()
    requested = Path(value)
    candidate = (requested if requested.is_absolute() else root / requested).resolve()
    if candidate.is_relative_to(root):
        return candidate
    project_source = Path(__file__).resolve().parents[2]
    route = library_root(project_source)
    # Only registered configuration directories may cross the old
    # project boundary. This does not grant access to arbitrary sibling files.
    if candidate.suffix in {'.yaml', '.yml'} and any(
        candidate.is_relative_to(folder.resolve()) for folder in
        (route / 'configs', route / 'covindb-configs', ligand_root(project_source) / 'configs', de_novo_root(project_source) / 'configs')
    ):
        return candidate
    raise ValueError('Configuration is outside the project and registered route configuration roots')


# Preserve callers and frozen plans using the original public helper name.
resolve_library_config = resolve_route_config


def config_reference(project: Path, value: str | Path) -> str:
    path = resolve_route_config(project, value)
    root = Path(project).resolve()
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def science_script(name: str) -> Path:
    """Executable identity follows installed code, never a mutable data directory."""
    if Path(name).name != name:
        raise ValueError('Expected a script basename')
    project = Path(__file__).resolve().parents[2]
    route = ligand_root(project) / 'scripts' / name
    denovo = de_novo_root(project) / 'scripts' / name
    path = route if route.is_file() else denovo if denovo.is_file() else project / 'scripts' / name
    if not path.is_file():
        raise FileNotFoundError(f'Missing installed science script: {name}')
    return path


def library_root(project: Path) -> Path:
    return Path(project).resolve().parent / 'routes/library-first'


def enable_library_first() -> Path:
    route = library_root(Path(__file__).resolve().parents[2])
    source = str(route / 'src')
    if source not in sys.path:
        sys.path.insert(0, source)
    return route


def library_script(project: Path, name: str) -> Path:
    if Path(name).name != name:
        raise ValueError('Expected a script basename')
    # The project argument can be a data-only campaign. Executable identity
    # follows this installed package, not the campaign's filesystem location.
    path = library_root(Path(__file__).resolve().parents[2]) / 'scripts' / name
    if not path.is_file():
        raise FileNotFoundError(f'Missing canonical Library-first script: {path}')
    return path


def ligand_root(project: Path) -> Path:
    return Path(project).resolve().parent / 'routes/ligand-first'


def enable_ligand_first() -> Path:
    route = ligand_root(Path(__file__).resolve().parents[2])
    source = str(route / 'src')
    if source not in sys.path:
        sys.path.insert(0, source)
    return route


def ligand_script(project: Path, name: str) -> Path:
    if Path(name).name != name:
        raise ValueError('Expected a script basename')
    route = ligand_root(Path(__file__).resolve().parents[2])
    for folder in ('scripts', 'historical-scripts'):
        path = route / folder / name
        if path.is_file():
            return path
    raise FileNotFoundError(f'Missing canonical Ligand-first script: {name}')


def de_novo_root(project: Path) -> Path:
    return Path(project).resolve().parent / 'routes/de-novo'


def enable_de_novo() -> Path:
    route = de_novo_root(Path(__file__).resolve().parents[2])
    source = str(route / 'src')
    if source not in sys.path:
        sys.path.insert(0, source)
    return route


def de_novo_script(name: str) -> Path:
    if Path(name).name != name:
        raise ValueError('Expected a script basename')
    route = de_novo_root(Path(__file__).resolve().parents[2])
    for folder in ('scripts', 'historical-scripts'):
        path = route / folder / name
        if path.is_file():
            return path
    raise FileNotFoundError(f'Missing canonical De novo script: {name}')
