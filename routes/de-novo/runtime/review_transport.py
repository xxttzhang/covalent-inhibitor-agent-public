"""Transport a host-frozen native-study source to the active shared data root.

Only regular, hash-bound data files are transferred. The receiver never executes
archive contents, never modifies a release, never submits a job and never follows
caller-selected remote paths. Existing source IDs must match byte for byte.
"""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile

MAX_BYTES = 256 * 1024 * 1024

REMOTE = r'''
import hashlib,io,json,os,pathlib,re,shutil,sys,tarfile,tempfile
root=pathlib.Path(sys.argv[1]); source_id=sys.argv[2]; checksum=sys.argv[3]
if not root.is_absolute() or not root.is_dir() or not re.fullmatch('[a-f0-9]{64}',checksum) or source_id!='denovo-review-'+checksum[:32]:raise ValueError('Invalid registered destination')
header=json.loads(sys.stdin.buffer.readline(2*1024*1024))
files=header['files']
if len(files)>10000 or files.get('source.json')!=checksum: raise ValueError('Invalid source inventory')
if any(not n or pathlib.PurePosixPath(n).is_absolute() or any(p in ('','.','..') for p in n.split('/')) for n in files): raise ValueError('Unsafe source path')
parent=root/'state/de-novo-inputs'
for p in (root,root/'state',parent):
 if p.is_symlink(): raise ValueError('Source parent is a symlink')
parent.mkdir(parents=True,exist_ok=True)
target=parent/source_id
stage=pathlib.Path(tempfile.mkdtemp(prefix='.incoming-',dir=parent))
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as stream:
  for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
 return h.hexdigest()
def check(folder):
 actual={}
 for p in folder.rglob('*'):
  if p.is_symlink() or not (p.is_dir() or p.is_file()):raise ValueError('Unsafe installed source')
  if p.is_file():actual[p.relative_to(folder).as_posix()]=digest(p)
 if actual!=files:raise ValueError('Installed source inventory changed')
try:
 total=0;seen=set()
 with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as archive:
  for item in archive:
   if not item.isfile() or item.name not in files or item.name in seen:raise ValueError('Unexpected archive member')
   total+=item.size
   if item.size<0 or total>268435456:raise ValueError('Archive size exceeded')
   seen.add(item.name); p=stage/item.name;p.parent.mkdir(parents=True,exist_ok=True)
   with p.open('xb') as out:shutil.copyfileobj(archive.extractfile(item),out)
   if digest(p)!=files[item.name]:raise ValueError('Transport checksum differs')
 if seen!=set(files):raise ValueError('Transport inventory incomplete')
 source=json.loads((stage/'source.json').read_text())
 if source['schema']!='native-denovo-independent-source-v1' or source['route']!='de-novo':raise ValueError('Source route differs')
 if target.exists() or target.is_symlink():
  if target.is_symlink() or not target.is_dir():raise ValueError('Source destination is unsafe')
  check(target)
 else:
  os.rename(stage,target)
 check(target)
 print(json.dumps({'state':'SOURCE_REGISTERED_NO_COMPUTE','source_id':source_id,'source_manifest_sha256':checksum,'study_id':source['study_id'],'data_root':str(root),'remote_directory':str(target),'file_count':len(files),'compute_submitted':False}))
finally:
 if stage.exists():shutil.rmtree(stage)
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_module(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package(project, study):
    project = Path(project).resolve(strict=True)
    route = Path(__file__).resolve().parents[1]
    profile = json.loads((route/'registry/egfr_c797_v1/profile.json').read_text())
    if str(project) not in profile['allowed_directories'] or not re.fullmatch(r'study-[a-f0-9-]{36}', study):
        raise ValueError('Native project/study is not registered')
    folder = project/'.discovery/studies'/study
    receipt_path = folder/'denovo-independent-input.json'
    if any(p.is_symlink() for p in (receipt_path, *receipt_path.parents)):
        raise ValueError('Native source receipt cannot traverse symbolic links')
    receipt = json.loads(receipt_path.read_text())
    root = folder/'denovo-independent-input'
    checksum = receipt['source_manifest_sha256']
    source_id = receipt['source_id']
    if (receipt.get('schema') != 'study-denovo-independent-input-v1' or receipt.get('study_id') != study
            or source_id != 'denovo-review-'+checksum[:32] or not re.fullmatch('[a-f0-9]{64}', checksum)
            or receipt.get('source_directory') != root.name or receipt.get('remote_source_registered') is not False):
        raise ValueError('Native source receipt identity changed')
    sys.path.insert(0, str(route/'evaluation'))
    api = load_module('denovo_transport_source', route/'evaluation/source.py')
    value = api.validate(root, checksum)
    if value['study_id'] != study or value['project_id'] != receipt['project_id']:
        raise ValueError('Source belongs to a different native study')
    files = {**value['files'], 'source.json': checksum}
    stream = io.BytesIO()
    stream.write((json.dumps({'files': files}, separators=(',', ':'))+'\n').encode())
    with tarfile.open(fileobj=stream, mode='w|') as archive:
        for name, expected in sorted(files.items()):
            file = api.regular(root, name, expected)
            data = file.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data); info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    if sum((root/name).stat().st_size for name in files) > MAX_BYTES:
        raise ValueError('Transport byte bound exceeded')
    return receipt, stream.getvalue()


def transport(project, study, workspace):
    workspace = Path(workspace).resolve(strict=True)
    project = Path(project).resolve(strict=True)
    if project not in {workspace, workspace/'openscience-custom'}:
        raise ValueError('Transport workspace must own the registered native project')
    bridge_path = workspace/'ops/hypothesis_remote_mcp.py'
    if any(p.is_symlink() for p in (bridge_path, *bridge_path.parents)):
        raise ValueError('Host bridge cannot traverse symlinks')
    bridge = load_module('denovo_registered_host_bridge', bridge_path)
    data_root, _, _ = bridge.discovery_runtime()
    receipt, archive = package(project, study)
    source_id, checksum = receipt['source_id'], receipt['source_manifest_sha256']
    command = shlex.join(['python3', '-B', '-c', REMOTE, data_root, source_id, checksum])
    completed = subprocess.run([*bridge.SSH, command], input=archive, capture_output=True, timeout=180)
    if completed.returncode:
        raise RuntimeError('Source transport failed; inspect before retry: '+completed.stderr.decode(errors='replace')[-2000:])
    result = json.loads(completed.stdout)
    if (result.get('source_id') != source_id or result.get('source_manifest_sha256') != checksum
            or result.get('study_id') != study or result.get('data_root') != data_root
            or result.get('compute_submitted') is not False):
        raise ValueError('Transport readback differs from native study source')
    result['bridge_sha256'] = sha(bridge_path)
    result['transport_script_sha256'] = sha(Path(__file__))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-directory', required=True)
    parser.add_argument('--study-id', required=True)
    parser.add_argument('--workspace', required=True)
    args = parser.parse_args()
    print(json.dumps(transport(args.project_directory, args.study_id, args.workspace)))
