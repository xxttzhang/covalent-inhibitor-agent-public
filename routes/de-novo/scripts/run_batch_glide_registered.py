"""Dispatch only an existing signed instance task."""
import argparse
from pathlib import Path
from covscreen.instance_glide_claim import execute
from covscreen.jobs import ArtifactRegistry, sha256_file

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    registry = ArtifactRegistry(args.root)
    digest = sha256_file(registry.job_dir(args.run_id) / 'spec.json')
    result = execute(args.root, args.run_id, digest, 'asgbie3')
    print(result.model_dump_json())
    raise SystemExit(0 if result.state == 'COMPLETED' else 1)
