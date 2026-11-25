"""Revalidate generation lineage in the registered scientific environment, no job submission."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[name] = '1'
root = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(root/'routes/de-novo/src'),
    str(root/'science'/'src')]
from covscreen_de_novo.independent_review import source_api
from covscreen_de_novo.prereaction_runner import runtime, encoded

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--source-sha256')
    args = parser.parse_args()
    api = source_api()
    binding = runtime()
    if args.probe:
        print(json.dumps({'source_verifier_imported': True,
            'runtime_sha256': hashlib.sha256(encoded(binding)).hexdigest(),
            'versions': binding['versions']}))
    else:
        if args.source is None or args.source_sha256 is None:
            parser.error('Exact source directory and manifest checksum required')
        if hasattr(os, 'sched_getaffinity'):
            os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
        value = api.validate(args.source, args.source_sha256)
        if runtime() != binding:
            raise ValueError('Source verification runtime changed during validation')
        print(json.dumps({'source': value, 'runtime_sha256': hashlib.sha256(encoded(binding)).hexdigest()},
                         allow_nan=False))
