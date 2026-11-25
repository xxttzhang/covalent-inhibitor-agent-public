#!/usr/bin/env python3
"""Freeze existing historical source files; never execute a scientific job.

The operator's JSON must contain sources (all roles required by FILES), system_id,
compound_id and input_sdf_index. The public workflow subsequently accepts only
the returned artifact ID and manifest hash, never these source paths.
"""
import argparse
import json
from pathlib import Path

import _bootstrap
from covscreen.md_historical_source import freeze


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--input', required=True)
    args = parser.parse_args()
    value = json.loads(Path(args.input).read_text())
    if set(value) != {'sources', 'system_id', 'compound_id', 'input_sdf_index'}:
        raise ValueError('Provide exactly the source inventory and candidate identity')
    print(json.dumps(freeze(Path(args.root), **value), ensure_ascii=False))
