"""Deliver sealed hypothesis evidence as data; no retrieval, inference or computation."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
from uuid import uuid4

MAX_BYTES = 64 * 1024 * 1024

def name(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,255}', value)
            or any(part in ('', '.', '..') or part.startswith('.') for part in value.split('/'))):
        raise ValueError('Invalid sealed evidence filename')
    return value

def unpack(blob, expected):
    if len(blob)>MAX_BYTES+2*1024*1024:
        raise ValueError('Evidence transfer exceeds bounded size')
    files={};total=0
    with tarfile.open(fileobj=io.BytesIO(blob),mode='r:') as archive:
        for entry in archive:
            key=name(entry.name)
            total+=entry.size
            if not entry.isfile() or key in files or entry.size<0 or total>MAX_BYTES or len(files)>=2000:
                raise ValueError('Unsafe sealed evidence inventory')
            files[key]=archive.extractfile(entry).read()
    raw=files.get('manifest.json')
    if raw is None or hashlib.sha256(raw).hexdigest()!=expected:
        raise ValueError('Sealed evidence manifest checksum differs')
    manifest=json.loads(raw)
    if (not isinstance(manifest.get('files'),dict) or 'evidence.json' not in manifest['files']
            or 'manifest.json' in manifest['files'] or set(files)!={'manifest.json',*manifest['files']}):
        raise ValueError('Sealed evidence file inventory differs')
    for key,digest in manifest['files'].items():
        name(key)
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest) or hashlib.sha256(files[key]).hexdigest()!=digest:
            raise ValueError('Sealed evidence file checksum differs: '+key)
    return files

def install(blob, identifier, expected, destination):
    if not re.fullmatch('hyp-[a-f0-9]{24}',identifier) or not re.fullmatch('[a-f0-9]{64}',expected):
        raise ValueError('Exact sealed evidence identity required')
    files=unpack(blob,expected)
    # The configured delivery directory is trusted. Canonicalize platform aliases
    # such as macOS /var -> /private/var before checking evidence-owned paths.
    base=Path(destination).resolve(strict=True)
    root=base/identifier
    for relative,data in files.items():
        path=root/relative
        if any(p.is_symlink() for p in (path,*path.parents) if p != base and base in p.parents):
            raise ValueError('Evidence delivery cannot traverse symbolic links')
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            if not path.is_file() or path.read_bytes()!=data:
                raise ValueError('Existing immutable evidence differs: '+relative)
            continue
        temporary=path.with_name('.evidence-transfer-'+uuid4().hex)
        try:
            with temporary.open('xb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            try:os.link(temporary,path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes()!=data:raise ValueError('Concurrent evidence conflict')
        finally:temporary.unlink(missing_ok=True)
    return {'evidence_id':identifier,'manifest_sha256':expected,'directory':str(root),
            'file_count':len(files),'compute_submitted':False}

REMOTE = r'''
import hashlib,io,json,pathlib,re,sys,tarfile
root=pathlib.Path(sys.argv[1]);identifier=sys.argv[2];expected=sys.argv[3]
assert re.fullmatch('hyp-[a-f0-9]{24}',identifier) and re.fullmatch('[a-f0-9]{64}',expected)
folder=root/'results/target-hypotheses'/identifier
def read(name):
 assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,255}',name)
 assert all(part not in ('','.','..') and not part.startswith('.') for part in name.split('/'))
 p=folder/name;assert not any(x.is_symlink() for x in (p,*p.parents)) and p.is_file()
 assert p.stat().st_size<=67108864
 return p.read_bytes()
raw=read('manifest.json');assert hashlib.sha256(raw).hexdigest()==expected
manifest=json.loads(raw);assert isinstance(manifest['files'],dict) and 'evidence.json' in manifest['files']
assert len(manifest['files'])<2000 and 'manifest.json' not in manifest['files']
files={'manifest.json':raw};total=len(raw)
for name,digest in manifest['files'].items():
 data=read(name);total+=len(data);assert total<=67108864 and hashlib.sha256(data).hexdigest()==digest
 files[name]=data
buffer=io.BytesIO()
with tarfile.open(fileobj=buffer,mode='w') as archive:
 for name,data in sorted(files.items()):
  entry=tarfile.TarInfo(name);entry.size=len(data);entry.mode=0o600;archive.addfile(entry,io.BytesIO(data))
assert read('manifest.json')==raw
sys.stdout.buffer.write(buffer.getvalue())
'''

def transfer(identifier, expected, data_root, destination, ssh):
    if not re.fullmatch('hyp-[a-f0-9]{24}',identifier) or not re.fullmatch('[a-f0-9]{64}',expected):
        raise ValueError('Exact sealed evidence identity required')
    command=shlex.join(['python3','-B','-c',REMOTE,data_root,identifier,expected])
    result=subprocess.run([*ssh,command],capture_output=True,timeout=90)
    if result.returncode:
        raise RuntimeError('Sealed evidence transfer failed: '+result.stderr.decode(errors='replace')[-1500:])
    return install(result.stdout,identifier,expected,destination)
