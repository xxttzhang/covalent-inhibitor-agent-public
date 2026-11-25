"""Connect one explicitly bound Ligand-first consumer without moving shared pointers.

The binding contains paths and hashes, never credentials. SSH forwards stdio
unchanged; a disconnected call is never replayed.
"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat

from hypothesis_remote_mcp import SSH, relay


def remote_command(binding, *, verify_runtime_config=False, runtime=None):
    required = {"release_id", "manifest_sha256", "release_root", "data_root"}
    if not isinstance(binding, dict) or set(binding) != required:
        raise ValueError("Expected an exact release/data binding")
    for name in ("release_id", "manifest_sha256"):
        if not isinstance(binding[name], str) or not re.fullmatch(r"[a-f0-9]{64}", binding[name]):
            raise ValueError("Invalid release digest")
    for name in ("release_root", "data_root"):
        value = binding[name]
        if (not isinstance(value, str) or not value.startswith("/inspire/")
                or str(PurePosixPath(value)) != value or ".." in PurePosixPath(value).parts
                or any(ord(char) < 32 for char in value)):
            raise ValueError("Expected a normalized absolute Inspire path")
    release, data = (PurePosixPath(binding[name]) for name in ("release_root", "data_root"))
    if release.name != binding["release_id"] or data == release or release in data.parents or data in release.parents:
        raise ValueError("Release identity and separate data roots are required")
    # Verify the manifest and verifier before executing any installed code.
    verification = f"""
import hashlib,json,os,pathlib,runpy,stat
binding={binding!r}
root=pathlib.Path(binding['release_root']); data=pathlib.Path(binding['data_root'])
if root.is_symlink() or data.is_symlink() or not data.is_dir():
    raise ValueError('Consumer roots must be existing regular directories')
raw=(root/'manifest.json').read_bytes()
if hashlib.sha256(raw).hexdigest()!=binding['manifest_sha256']:
    raise ValueError('Release manifest changed')
manifest=json.loads(raw); verifier=root/'code_release.py'; source=verifier.read_bytes()
if verifier.is_symlink() or hashlib.sha256(source).hexdigest()!=manifest['files']['code_release.py']:
    raise ValueError('Release verifier changed')
namespace={{'__name__':'release_verifier','__file__':str(verifier)}}
exec(compile(source,str(verifier),'exec'),namespace)
namespace['verify'](root,binding['release_id'],binding['manifest_sha256'])
key=data/'.secrets/native-approval.key'
info=key.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size<32:
    raise ValueError('Consumer approval key must be an owner-only regular file')
"""
    execution = """
os.environ['COVSCREEN_PROJECT_ROOT']=str(data)
os.environ['COVALENT_APPROVAL_KEY_FILE']=str(key)
os.environ['PYTHONDONTWRITEBYTECODE']='1'
os.environ['PYTHONNOUSERSITE']='1'
os.chdir(data)
runpy.run_path(str(root/'science/scripts/openscience_mcp_server.py'),run_name='__main__')
"""
    if verify_runtime_config:
        check = """
config=data/'configs/local.inspire.yaml'
if config.is_symlink() or not config.is_file() or config.read_bytes()!=(root/'science/configs/local.inspire.yaml').read_bytes():
    raise ValueError('Consumer runtime configuration differs from the frozen release')
"""
        verification += check
    if runtime is not None:
        if not isinstance(runtime, dict) or set(runtime) not in ({'python', 'sha256', 'lock', 'lock_sha256'}, {'python', 'sha256', 'lock', 'lock_sha256', 'covalentizer'}):
            raise ValueError('Expected an exact scientific host runtime binding')
        for name in ('python', 'lock'):
            value = runtime[name]
            if (not isinstance(value, str) or not value.startswith('/inspire/')
                    or str(PurePosixPath(value)) != value or '..' in PurePosixPath(value).parts
                    or any(ord(char) < 32 for char in value)):
                raise ValueError('Invalid scientific host runtime path')
        for name in ('sha256', 'lock_sha256'):
            if not isinstance(runtime[name], str) or not re.fullmatch(r'[a-f0-9]{64}', runtime[name]):
                raise ValueError('Invalid scientific host runtime digest')
        verification += f"""
runtime={runtime!r}
for filename,checksum in ((runtime['python'],runtime['sha256']), (runtime['lock'],runtime['lock_sha256'])):
    if hashlib.sha256(pathlib.Path(filename).read_bytes()).hexdigest()!=checksum:
        raise ValueError('Scientific host runtime asset changed')
if not os.access(runtime['python'],os.X_OK):
    raise ValueError('Scientific host Python is not executable')
"""
    if runtime is not None and 'covalentizer' in runtime:
        asset = runtime['covalentizer']
        names = {'Code/CovUtils.py', 'Code/numbered_reaction.re', 'Scripts/reform_elec.re'}
        if not isinstance(asset, dict) or set(asset) != {'root', 'files'}:
            raise ValueError('Expected exact Covalentizer asset binding')
        value = asset['root']
        if (not isinstance(value, str) or not value.startswith('/inspire/')
                or str(PurePosixPath(value)) != value or '..' in PurePosixPath(value).parts
                or any(ord(char) < 32 for char in value)):
            raise ValueError('Invalid Covalentizer asset root')
        if (not isinstance(asset['files'], dict) or set(asset['files']) != names
                or any(not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
                       for digest in asset['files'].values())):
            raise ValueError('Invalid Covalentizer asset digests')
        verification += f"""
asset={asset!r}
asset_root=pathlib.Path(asset['root'])
for name,digest in asset['files'].items():
    path=asset_root/name
    if any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file():
        raise ValueError('Covalentizer asset must be a regular file')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise ValueError('Covalentizer asset changed')
"""
        execution = "os.environ['COVALENTIZER']=asset['root']\n" + execution
    instance = os.environ.get('COVALENT_LIGAND_VS_INSTANCE', '')
    if instance and instance not in {'asgbie3', 'asgbie4'}:
        raise ValueError('Unregistered Ligand CPU execution instance')
    if instance:
        execution = f"os.environ['COVALENT_LIGAND_VS_INSTANCE']={instance!r}\n" + execution
    program = verification + execution
    if runtime is not None:
        return ('python3 -B -c ' + shlex.quote(verification)
                + ' && exec env -u PYTHONHOME -u PYTHONPATH -u LD_PRELOAD -u LD_AUDIT -u LD_LIBRARY_PATH '
                + shlex.quote(runtime['python']) + ' -u -B -c ' + shlex.quote(program))
    activation = str(release / "science/environment/activate_carsidock.sh")
    return ("python3 -B -c " + shlex.quote(verification) + " && source "
            + shlex.quote(activation) + " >/dev/null && exec python -u -B -c " + shlex.quote(program))


def read_profile(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('Installation profile must be an owner-only regular file')
    return json.loads(path.read_text())


def connection(path):
    """Installation-owned SSH profile; never an MCP/model-supplied argument."""
    value = read_profile(path)
    if set(value) != {'notebook', 'workspace', 'account', 'cli', 'identity_file', 'port'}:
        raise ValueError('Expected an exact notebook connection profile')
    if type(value['port']) is not int or not 1 <= value['port'] <= 65535:
        raise ValueError('Invalid SSH port')
    for name in ('notebook', 'workspace', 'account', 'cli', 'identity_file'):
        if not isinstance(value[name], str) or not value[name] or any(ord(c) < 32 for c in value[name]):
            raise ValueError('Invalid connection field: ' + name)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', value['notebook']):
        raise ValueError('Invalid notebook name')
    for name in ('cli', 'identity_file'):
        if not Path(value[name]).is_absolute() or not Path(value[name]).is_file():
            raise ValueError('Missing registered connection asset: ' + name)
    proxy = shlex.join([value['cli'], 'notebook', 'ssh-proxy', '%h', '--account', value['account'],
                       '--workspace', value['workspace'], '--port', '%p'])
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-o', 'StrictHostKeyChecking=accept-new', '-i', value['identity_file'],
            '-o', 'ProxyCommand=' + proxy, '-p', str(value['port']), 'root@' + value['notebook']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--connection", type=Path, help="Owner-only installation SSH profile")
    parser.add_argument("--verify-runtime-config", action="store_true")
    parser.add_argument("--runtime", type=Path, help="Owner-only scientific host runtime binding")
    parser.add_argument("--delivery-root", type=Path, help="Explicit local project hypothesis delivery directory")
    args = parser.parse_args()
    binding = read_profile(args.binding)
    command = remote_command(binding, verify_runtime_config=args.verify_runtime_config,
                             runtime=read_profile(args.runtime) if args.runtime else None)
    ssh = connection(args.connection) if args.connection else SSH
    if not ssh:
        raise ValueError('Provide --connection or configure COVSCREEN_BRIDGE_CONFIG')
    if args.delivery_root:
        if not args.delivery_root.is_absolute():
            raise ValueError('Explicit absolute delivery directory required')
        args.delivery_root.mkdir(parents=True, exist_ok=True)
        if args.delivery_root.resolve() != args.delivery_root:
            raise ValueError('Delivery directory cannot traverse symbolic links')
        relay(command, binding['data_root'], ssh=ssh, delivery=args.delivery_root,
              allowed_roots=(binding['data_root'],))
    os.execvp(ssh[0], ssh + [command])


if __name__ == "__main__":
    main()
