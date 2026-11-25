"""Register a frozen PocketXMol batch using the existing host SSH bridge."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from covscreen_de_novo.batch_input import validate

REMOTE = r'''
import base64,hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); payload=json.load(sys.stdin)
files=payload['files']; checksum=payload['manifest_sha256']
if set(files)!={'input.json','binding.json','ligands.sdf','receptor.pdb'}:raise ValueError('Unexpected batch inventory')
decoded={name:base64.b64decode(value,validate=True) for name,value in files.items()}
if sum(map(len,decoded.values()))>67108864:raise ValueError('Batch too large')
if hashlib.sha256(decoded['input.json']).hexdigest()!=checksum:raise ValueError('Manifest checksum mismatch')
manifest=json.loads(decoded['input.json']); binding=json.loads(decoded['binding.json'])
if manifest['schema']!='pocketxmol-batch-input-v1' or binding['manifest_sha256']!=checksum:raise ValueError('Batch identity mismatch')
if hashlib.sha256(decoded['ligands.sdf']).hexdigest()!=manifest['ligands_sha256'] or hashlib.sha256(decoded['receptor.pdb']).hexdigest()!=binding['receptor_sha256']:raise ValueError('Batch file mismatch')
target=root/'state/de-novo-batches'/('batch-'+checksum)
if not root.is_absolute() or not root.is_dir() or any(p.is_symlink() for p in (target,*target.parents)):raise ValueError('Unsafe destination')
target.mkdir(parents=True,exist_ok=True)
for name,data in decoded.items():
 p=target/name
 if p.is_symlink():raise ValueError('Unsafe existing batch file')
 try:
  with p.open('xb') as stream:stream.write(data)
 except FileExistsError:
  if not p.is_file() or p.read_bytes()!=data:raise ValueError('Existing batch differs')
if any((target/name).read_bytes()!=data for name,data in decoded.items()):raise ValueError('Registered bytes differ')
print(json.dumps({'state':'BATCH_REGISTERED_NO_COMPUTE','task_id':manifest['task_id'],'manifest_sha256':checksum,'remote_directory':str(target),'compute_submitted':False}))
'''


def transport(folder, checksum, workspace):
    manifest, _ = validate(folder, checksum)
    workspace = Path(workspace).resolve(strict=True)
    runtime = os.environ.get('COVALENT_RUNTIME_ROOT')
    if not runtime or not Path(runtime).is_absolute():
        raise ValueError('Explicit acceptance runtime required for batch registration')
    runtime = Path(runtime).resolve(strict=True)
    if json.loads((runtime / 'binding.json').read_text()).get('source') != str(workspace):
        raise ValueError('Batch transport runtime belongs to a different source')
    sys.path.insert(0, str(workspace / 'ops'))
    bridge_path = workspace / 'ops/ligand_bound_mcp.py'
    spec = importlib.util.spec_from_file_location('batch_host_bridge', bridge_path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    binding = bridge.read_profile(runtime / 'config/science-binding.json')
    bridge.remote_command(binding)  # Validate the same exact binding as the connector.
    root = binding['data_root']
    ssh = bridge.connection(runtime / 'config/science-connection.json')
    files = {name: (Path(folder) / name).read_bytes()
             for name in ('input.json', 'binding.json', 'ligands.sdf', 'receptor.pdb')}
    if sum(map(len, files.values())) > 64 * 1024 * 1024:
        raise ValueError('Batch input exceeds transport limit')
    if hashlib.sha256(files['input.json']).hexdigest() != checksum:
        raise ValueError('Batch changed during packaging')
    payload = {'manifest_sha256': checksum,
               'files': {name: base64.b64encode(data).decode() for name, data in files.items()}}
    command = shlex.join(['python3', '-B', '-c', REMOTE, root])
    result = subprocess.run([*ssh, command], input=json.dumps(payload).encode(),
                            capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError('Batch transport failed: ' + result.stderr.decode(errors='replace')[-2000:])
    value = json.loads(result.stdout)
    if (value.get('manifest_sha256') != checksum or value.get('task_id') != manifest['task_id']
            or value.get('remote_directory') != str(Path(root) / 'state/de-novo-batches' / ('batch-' + checksum))
            or value.get('compute_submitted') is not False):
        raise ValueError('Batch registration receipt differs')
    return value


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(transport(args.input, args.manifest_sha256, args.workspace)))
