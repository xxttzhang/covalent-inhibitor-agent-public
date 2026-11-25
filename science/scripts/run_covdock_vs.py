"""Registered VS worker entry; never accepts model-supplied shell commands."""
import argparse
from pathlib import Path

from covscreen.covdock_vs_runner import run


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root',type=Path,required=True)
    parser.add_argument('--run-id',required=True)
    args = parser.parse_args()
    run(args.project_root,args.run_id)
