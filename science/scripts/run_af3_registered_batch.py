from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from covscreen.af3_selection import select_af3_candidates
from covscreen.jobs import sha256_file, utc_now
from covscreen.refinement_input import verify_refinement_input, verify_refinement_identities
from covscreen.refinement_output import freeze_refinement_outputs
from covscreen.af3_target import read_target_seed, validate_target
from parse_af3_batch import parse_batch


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _successful_run(manifest: Path, output: Path, input_json: Path) -> bool:
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if payload.get("status") == "SUCCESS" and (
            not input_json.is_file() or payload.get("input_json_sha256") != sha256_file(input_json)):
        raise ValueError("AF3 successful prediction belongs to a different or unverifiable prepared input")
    return payload.get("status") == "SUCCESS" and bool(
        list(output.rglob("*_model.cif"))
        and list(output.rglob("*_summary_confidences.json"))
    )


def _verify_reuse_source(directory: Path, seed_check: dict, candidate_table: Path) -> None:
    manifest = directory / "stage4_selection_manifest.json"
    if not manifest.is_file():
        raise ValueError("AF3 reuse requires a source selection manifest")
    inputs = json.loads(manifest.read_text()).get("inputs", {})
    expected = {"source_data_json": seed_check["source_data_sha256"],
                "target_config": seed_check["target_config_sha256"],
                "candidate_table": sha256_file(candidate_table)}
    for field, checksum in expected.items():
        item = inputs.get(field)
        if not isinstance(item, dict) or item.get("sha256") != checksum:
            raise ValueError(f"AF3 reused artifacts differ from current inputs: {field}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare, resume, run, parse, and rank a registered AF3-mPAE discovery batch."
    )
    parser.add_argument("--candidate-table", type=Path, required=True)
    parser.add_argument("--source-data-json", type=Path, required=True)
    parser.add_argument("--target-config", type=Path, required=True)
    parser.add_argument("--expected-source-data-sha256")
    parser.add_argument("--expected-target-config-sha256")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--af3-python", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--database-dir", type=Path, required=True)
    parser.add_argument("--protein-chain", default="A")
    parser.add_argument("--ligand-chain", default="L")
    parser.add_argument("--cysteine-sequence-index", type=int, required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--source-residue-number", type=int, required=True)
    parser.add_argument("--nucleophilic-atom", default="SG")
    parser.add_argument("--top-n", type=int, default=100, help="Post-inference retention count, NOT an input/compute cap")
    parser.add_argument("--expected-input-count", type=int)
    parser.add_argument("--expected-input-sha256")
    parser.add_argument("--expected-identity-sha256")
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--num-recycles", type=int, default=3)
    parser.add_argument("--num-diffusion-samples", type=int, default=1)
    parser.add_argument("--num-seeds", type=int, default=1)
    parser.add_argument("--cpu-per-search", type=int, default=2)
    parser.add_argument("--random-seed", type=int, default=20260725)
    parser.add_argument("--min-coverage", type=float, default=0.95)
    parser.add_argument("--run-data-pipeline", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--reuse-artifact-dir",
        type=Path,
        help=(
            "Reuse prepared inputs and successful predictions from a previous "
            "registered AF3 run and only repeat parsing, quality gates, and ranking."
        ),
    )
    args = parser.parse_args()
    input_contract = verify_refinement_input(args.candidate_table, args.expected_input_count,
                                            args.expected_input_sha256, args.expected_identity_sha256)
    target_overrides = {"target_id": args.target_id, "protein_chain": args.protein_chain,
        "ligand_chain": args.ligand_chain, "cysteine_sequence_index": args.cysteine_sequence_index,
        "residue_number": args.source_residue_number, "nucleophilic_atom": args.nucleophilic_atom}
    seed_check = read_target_seed(args.source_data_json, args.target_config,
        expected_source_sha256=args.expected_source_data_sha256,
        expected_config_sha256=args.expected_target_config_sha256, overrides=target_overrides)
    reuse_artifacts = args.reuse_artifact_dir.resolve() if args.reuse_artifact_dir else None
    if reuse_artifacts is not None:
        _verify_reuse_source(reuse_artifacts, seed_check, args.candidate_table)

    paths = (
        args.candidate_table,
        args.source_data_json,
        args.target_config,
        args.runner,
        args.af3_python,
        args.model_dir,
        args.database_dir,
    )
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SystemExit(f"Missing AF3 batch inputs or assets: {missing}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prepared = (reuse_artifacts or args.output_dir) / "prepared"
    results = (reuse_artifacts or args.output_dir) / "predictions"
    results.mkdir(parents=True, exist_ok=True)
    progress_path = args.output_dir / "progress.json"
    started = time.monotonic()

    prepare_command = [
        sys.executable,
        str(Path(__file__).with_name("prepare_af3_covalent_batch.py")),
        "--af3-python",
        str(args.af3_python),
        "--library-table",
        str(args.candidate_table),
        "--source-data-json",
        str(args.source_data_json),
        "--expected-source-data-sha256",
        seed_check["source_data_sha256"],
        "--all-valid",
        "--protein-chain",
        args.protein_chain,
        "--ligand-chain",
        args.ligand_chain,
        "--cysteine-sequence-index",
        str(args.cysteine_sequence_index),
        "--target-id",
        args.target_id,
        "--source-residue-number",
        str(args.source_residue_number),
        "--nucleophilic-atom",
        args.nucleophilic_atom,
        "--random-seed",
        str(args.random_seed),
        "--output-dir",
        str(prepared),
    ]
    if args.resume:
        prepare_command.append("--resume")
    batch_manifest = prepared / "batch_manifest.json"
    if reuse_artifacts is not None:
        if not batch_manifest.is_file() or not results.is_dir():
            raise SystemExit(
                "Reusable AF3 artifacts must contain prepared/batch_manifest.json "
                "and predictions/"
            )
    else:
        preparation = subprocess.run(prepare_command, check=False)
        if preparation.returncode != 0 or not batch_manifest.is_file():
            _atomic_json(
                progress_path,
                {
                    "status": "preparation_failed",
                    "updated_at": utc_now(),
                    "return_code": preparation.returncode,
                },
            )
            return 2

    identities = json.loads(batch_manifest.read_text())
    # Preparation must neither expand nor silently shrink a contracted input.
    if input_contract is not None:
        verify_refinement_input(args.candidate_table, args.expected_input_count,
                                args.expected_input_sha256, args.expected_identity_sha256)
        verify_refinement_identities(identities, args.expected_input_count, args.expected_identity_sha256)
    # Reused preparation JSON must still contain the selected chain/sequence
    # and declared Cys-ligand connection, before any prediction is reused/run.
    for identity in identities:
        if str(identity.get("status", "SUCCESS")) != "SUCCESS":
            continue
        prepared_input = json.loads(Path(identity["input_json"]).read_text())
        validate_target(prepared_input, args.protein_chain, args.cysteine_sequence_index,
            args.ligand_chain, args.nucleophilic_atom, seed_check["protein_sequence_sha256"])
        expected_bond = [[args.protein_chain, args.cysteine_sequence_index, args.nucleophilic_atom],
                         [args.ligand_chain, 1, identity.get("electrophilic_atom")]]
        if (not identity.get("electrophilic_atom") or not any(
                bond == expected_bond or bond == expected_bond[::-1]
                for bond in prepared_input.get("bondedAtomPairs", []))):
            raise ValueError("AF3 prepared input lacks its declared target-ligand connection")
    completed = 0
    failed = 0
    skipped = 0
    preparation_failed = sum(
        str(identity.get("status", "SUCCESS")) != "SUCCESS"
        for identity in identities
    )
    for position, identity in enumerate(identities, start=1):
        if str(identity.get("status", "SUCCESS")) != "SUCCESS":
            continue
        job_name = str(identity["job_name"])
        output = results / job_name
        run_manifest = output.with_suffix(".run_manifest.json")
        input_json = Path(identity["input_json"])
        if reuse_artifacts is not None:
            if _successful_run(run_manifest, output, input_json):
                skipped += 1
                completed += 1
            else:
                failed += 1
        elif args.resume and _successful_run(run_manifest, output, input_json):
            skipped += 1
            completed += 1
        else:
            command = [
                sys.executable,
                str(Path(__file__).with_name("run_af3_job.py")),
                "--json-path",
                str(identity["input_json"]),
                "--output-dir",
                str(output),
                "--runner",
                str(args.runner),
                "--af3-python",
                str(args.af3_python),
                "--model-dir",
                str(args.model_dir),
                "--database-dir",
                str(args.database_dir),
                "--gpu",
                str(args.gpu),
                "--num-recycles",
                str(args.num_recycles),
                "--num-diffusion-samples",
                str(args.num_diffusion_samples),
                "--num-seeds",
                str(args.num_seeds),
                "--cpu-per-search",
                str(args.cpu_per_search),
                "--jax-cache-dir",
                str(args.output_dir / "jax_cache"),
            ]
            if not args.run_data_pipeline:
                command.append("--skip-data-pipeline")
            execution = subprocess.run(command, check=False)
            if execution.returncode == 0 and _successful_run(run_manifest, output, input_json):
                completed += 1
            else:
                failed += 1
        _atomic_json(
            progress_path,
            {
                "status": "running",
                "updated_at": utc_now(),
                "total": len(identities),
                "position": position,
                "completed": completed,
                "failed": failed,
                "skipped_on_resume": skipped,
                "preparation_failed": preparation_failed,
            },
        )

    # The configuration is also used by parsing; a changed target must not
    # reinterpret completed predictions or acquire a fresh provenance hash.
    read_target_seed(args.source_data_json, args.target_config,
        expected_source_sha256=seed_check["source_data_sha256"],
        expected_config_sha256=seed_check["target_config_sha256"], overrides=target_overrides)
    scores = parse_batch(
        None,
        results,
        args.target_config,
        batch_manifest=batch_manifest,
    )
    manifest = select_af3_candidates(
        scores,
        args.output_dir,
        top_n=args.top_n,
        min_coverage=args.min_coverage,
    )
    manifest["execution"] = {
        "runtime_seconds": time.monotonic() - started,
        "total": len(identities),
        "completed": completed,
        "failed": failed,
        "skipped_on_resume": skipped,
        "preparation_failed": preparation_failed,
        "resume_enabled": args.resume,
        "run_data_pipeline": args.run_data_pipeline,
        "gpu": args.gpu,
        "num_recycles": args.num_recycles,
        "num_diffusion_samples": args.num_diffusion_samples,
        "num_seeds": args.num_seeds,
        "cpu_per_search": args.cpu_per_search,
        "reparse_only": reuse_artifacts is not None,
        "reused_artifact_dir": str(reuse_artifacts) if reuse_artifacts else None,
    }
    manifest["inputs"] = {
        "af3_target_seed_check": seed_check,
        "refinement_input_contract": input_contract,
        "candidate_table": {
            "path": str(args.candidate_table),
            "sha256": sha256_file(args.candidate_table),
        },
        "source_data_json": {
            "path": str(args.source_data_json),
            "sha256": sha256_file(args.source_data_json),
        },
        "target_config": {
            "path": str(args.target_config),
            "sha256": sha256_file(args.target_config),
        },
        "batch_manifest": {
            "path": str(batch_manifest),
            "sha256": sha256_file(batch_manifest),
        },
    }
    if reuse_artifacts is not None:
        source_selection = reuse_artifacts / "stage4_selection_manifest.json"
        manifest["inputs"]["recovery_source"] = {
            "path": str(reuse_artifacts),
            "stage4_selection_manifest_sha256": (
                sha256_file(source_selection) if source_selection.is_file() else None
            ),
        }
    _atomic_json(args.output_dir / "stage4_selection_manifest.json", manifest)
    if input_contract is not None:
        manifest["refinement_artifacts"] = freeze_refinement_outputs(args.output_dir, "af3", input_contract)
    _atomic_json(
        progress_path,
        {
            "status": manifest["status"],
            "updated_at": utc_now(),
            "total": len(identities),
            "completed": completed,
            "failed": failed,
            "skipped_on_resume": skipped,
            "preparation_failed": preparation_failed,
            "quality_gates": manifest["quality_gates"],
        },
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
