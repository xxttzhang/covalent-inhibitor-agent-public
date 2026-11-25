"""Apply one hash-pinned compatibility update; no authentication or compute."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile

ORIGINAL = "63588bd14950d5b321c1a4ec0329e52533684046fb59a4a50fac308d1ea0f569"
BOOTSTRAP = "7373de37f589b932a89c392f4cc240e298f095333588e8c4d226539720089de0"
INITIAL_COMPAT = "29ac4753d59163150a6e7f437dad2579285a126da23a969d730c7d509b98facf"


def bootstrap(source):
    """Change only workspace discovery, leaving authentication flows intact."""
    source = source.replace('USER_ROUTES_PATH = "/api/v2/user?Action=GetRoutes"',
                            'USER_ROUTES_PATH = "/api/v2/user?Action=ListAccessibleWorkspaces"')
    source = source.replace('BOOTSTRAP_ROUTES_BODY = {"WorkspaceId": "default"}',
                            'BOOTSTRAP_ROUTES_BODY = {}')
    begin = source.index('# `GetRoutes` takes the literal `default`')
    end = source.index('USER_DETAIL_PATH', begin)
    source = source[:begin] + '# Current console enumerates accessible workspaces independently of routes.\n' + source[end:]
    begin = source.index('    workspace_ids: list[str] = []', source.index('def _workspace_routes_from_payload('))
    end = source.index('\n\ndef _merge_workspace_routes(', begin)
    return source[:begin] + '''    # Lazy import avoids the session/browser_api initialization cycle.
    from inspire.platform.web.browser_api.workspaces import _accessible_workspace_entries
    entries = _accessible_workspace_entries(_v2_result(payload))
    return (list(entries), {key: row["name"] for key, row in entries.items()},
            {key: row["is_fair_workspace"] for key, row in entries.items()})
''' + source[end:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--update-compat", action="store_true", help="Update the exact initial compatibility patch")
    args = parser.parse_args()
    target = args.target
    if target.is_symlink() or not target.is_file():
        raise ValueError("Target must be a regular installed module")
    original = target.read_bytes()
    candidate = (bootstrap(original.decode()).encode() if args.bootstrap
                 else Path(__file__).with_name("workspaces.py").read_bytes())
    compile(candidate, str(target), "exec")
    if args.bootstrap and args.update_compat:
        raise ValueError("Choose one module update")
    expected = BOOTSTRAP if args.bootstrap else INITIAL_COMPAT if args.update_compat else ORIGINAL
    if hashlib.sha256(original).hexdigest() != expected:
        raise ValueError("Installed module changed; review version before applying")
    if args.backup.exists():
        raise ValueError("Backup already exists; inspect prior installation")
    print("Validated exact installed module and candidate syntax; compute not submitted")
    if not args.apply:
        return
    args.backup.parent.mkdir(parents=True, exist_ok=True)
    with args.backup.open("xb") as backup:
        backup.write(original)
    args.backup.chmod(0o600)
    fd, temporary = tempfile.mkstemp(prefix=".workspace-compat-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, target.stat().st_mode & 0o777)
        if target.read_bytes() != original:
            raise ValueError("Installed module changed during preparation")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    assert target.read_bytes() == candidate
    print("Installed compatibility module sha256=" + hashlib.sha256(candidate).hexdigest())


if __name__ == "__main__":
    main()
