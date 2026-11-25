#!/usr/bin/env python3
"""Thin OpenMM/PDBFixer phase runner for registered receptor preparation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from covscreen.receptor_preparation import build_receptor_heavy_atoms
from covscreen.receptor_relaxation import relax_receptor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('heavy', 'relax'), required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    if args.mode == 'heavy':
        entity = json.loads(Path(request['entity']).read_text())
        instance = json.loads(Path(request['instance']).read_text())
        sequence = ''.join(Path(request['fasta']).read_text().splitlines()[1:])
        result = build_receptor_heavy_atoms(Path(request['source']), request['source_sha256'],
            entity, instance, request['uniprot'], request['cys'], sequence, request['taxon'],
            altlocs=request['altlocs'], model=request.get('model'), output=args.output,
            seed=request['seed'], max_missing_residues=request['max_missing_residues'],
            environment_policy='isolated_protein_proposal')
    else:
        policy = request.get('relaxation_policy')
        if policy is not None:
            digest = hashlib.sha256(json.dumps(policy, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            if request.get('relaxation_policy_sha256') != digest:
                raise ValueError('Relaxation policy must match the frozen request digest')
        elif 'relaxation_policy_sha256' in request:
            raise ValueError('Relaxation policy digest requires an explicit policy')
        result = relax_receptor(Path(request['parent']), request['parent_receipt_sha256'],
            Path(request['chirality_plan']), request['chirality_plan_sha256'],
            Path(request['source']), args.output, policy=policy)
    print(json.dumps({'status': result['status'], 'output': str(args.output)}, allow_nan=False))


if __name__ == '__main__':
    main()
