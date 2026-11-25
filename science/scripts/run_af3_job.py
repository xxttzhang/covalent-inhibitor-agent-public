from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from covscreen.adapters.alphafold3 import AlphaFold3Adapter
from covscreen.jobs import sha256_file
from covscreen.af3_environment import af3_environment, af3_site_packages


def hash_parsed_outputs(parsed: dict[str, list[str]]) -> dict[str, dict[str, str]]:
    return {
        category: {
            str(path): sha256_file(Path(path))
            for path in paths
            if Path(path).is_file()
        }
        for category, paths in parsed.items()
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-path", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--runner", type=Path, required=True)
    ap.add_argument("--af3-python", type=Path, required=True)
    ap.add_argument("--model-dir", type=Path, required=True)
    ap.add_argument("--database-dir", type=Path, required=True)
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--num-recycles", type=int, default=3)
    ap.add_argument("--num-diffusion-samples", type=int, default=1)
    ap.add_argument("--num-seeds", type=int, default=1)
    ap.add_argument("--cpu-per-search", type=int, default=2)
    ap.add_argument("--skip-data-pipeline", action="store_true")
    ap.add_argument("--jax-cache-dir", type=Path)
    args = ap.parse_args()

    json_path = args.json_path.resolve()
    output_dir = args.output_dir.resolve()
    jax_cache_dir = (
        args.jax_cache_dir.resolve()
        if args.jax_cache_dir
        else (output_dir.parent / "af3_jax_cache").resolve()
    )
    adapter = AlphaFold3Adapter(
        runner=str(args.runner.resolve()),
        python=str(args.af3_python.resolve()),
        model_dir=str(args.model_dir.resolve()),
        database_dir=str(args.database_dir.resolve()),
    )
    availability = adapter.check_availability()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir.with_suffix(".run_manifest.json")
    command = adapter.build_command(
        json_path=str(json_path),
        output_dir=str(output_dir),
        gpu_device=0,
        num_recycles=args.num_recycles,
        num_diffusion_samples=args.num_diffusion_samples,
        num_seeds=args.num_seeds,
        cpu_per_search=args.cpu_per_search,
        jax_cache_dir=str(jax_cache_dir),
        run_data_pipeline=not args.skip_data_pipeline,
    )
    manifest = {
        "engine": adapter.name,
        "input_json": str(json_path),
        "input_json_sha256": sha256_file(json_path),
        "output_dir": str(output_dir),
        "physical_gpu": args.gpu,
        "run_data_pipeline": not args.skip_data_pipeline,
        "command": command,
        "availability": availability,
        "status": "UNAVAILABLE",
    }
    if not availability["ready"]:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        return 2

    env = af3_environment(
        adapter.python, Path(__file__).resolve().parents[1] / "src", os.environ
    )
    env.update(
        {
            "CUDA_VISIBLE_DEVICES": str(args.gpu),
            "PYTHONUNBUFFERED": "1",
            "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
        }
    )
    stdout_path = output_dir.with_suffix(".stdout.log")
    stderr_path = output_dir.with_suffix(".stderr.log")
    start = time.monotonic()
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        proc = subprocess.run(
            command,
            cwd=adapter.runner.parent,
            env=env,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    parsed = adapter.parse_output(output_dir)
    manifest.update(
        {
            "runtime_seconds": time.monotonic() - start,
            "return_code": proc.returncode,
            "stdout_file": str(stdout_path),
            "stderr_file": str(stderr_path),
            "parsed_outputs": parsed,
            "parsed_output_sha256": hash_parsed_outputs(parsed),
            "status": (
                "SUCCESS"
                if proc.returncode == 0 and adapter.validate_output(parsed)
                else "FAILED"
            ),
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return 0 if manifest["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
