from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from covscreen.research import (
    LibraryFirstRequest,
    ProjectSeed,
    create_project,
    read_json,
    validate_library_first_request,
    validate_project,
    write_schemas,
)


def emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create and audit target-independent covalent discovery projects."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--seed", type=Path, required=True)
    create.add_argument("--projects-root", type=Path, default=Path("projects"))
    validate = commands.add_parser("validate")
    validate.add_argument("--project-dir", type=Path, required=True)
    schemas = commands.add_parser("write-schemas")
    schemas.add_argument("--schema-dir", type=Path, default=Path("schemas"))
    request = commands.add_parser("validate-library-first")
    request.add_argument("--project-dir", type=Path, required=True)
    request.add_argument("--request", type=Path, required=True)
    request.add_argument("--allow-missing-inputs", action="store_true")
    args = parser.parse_args()

    try:
        if args.command == "create":
            seed = ProjectSeed.model_validate(read_json(args.seed))
            result = {"status": "PASS", "project_dir": str(create_project(seed, args.projects_root))}
        elif args.command == "validate":
            result = validate_project(args.project_dir)
        elif args.command == "write-schemas":
            result = {"status": "PASS", "schemas": write_schemas(args.schema_dir)}
        else:
            model = LibraryFirstRequest.model_validate(read_json(args.request))
            result = validate_library_first_request(
                model, args.project_dir, require_inputs=not args.allow_missing_inputs
            )
    except Exception as exc:
        result = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    emit(result)
    return 0 if isinstance(result, dict) and result.get("status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
