from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import yaml

from covscreen.adapters.alphafold3 import AlphaFold3Adapter
from covscreen.schrodinger import sha256_file, validate_license_config


def module_version(name: str) -> str | None:
    try:
        module = __import__(name)
        return str(getattr(module, "__version__", "importable"))
    except Exception:
        return None


def file_report(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "exists": path.is_file(),
        "sha256": sha256_file(path) if path.is_file() else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--config", type=Path, default=Path("configs/global.yaml"))
    ap.add_argument("--require-gpu", action="store_true")
    args = ap.parse_args()
    config = yaml.safe_load(args.config.read_text())
    carsidock = config["carsidock"]
    af3 = config["alphafold3"]
    schrodinger = config["schrodinger"]

    module_names = [
        "rdkit",
        "numpy",
        "pandas",
        "pyarrow",
        "scipy",
        "sklearn",
        "pydantic",
        "yaml",
        "openpyxl",
        "Bio",
        "prody",
        "pytest",
        "torch",
        "dgl",
        "pytorch_lightning",
        "transformers",
        "torchmetrics",
        "spyrmsd",
        "MDAnalysis",
    ]
    report: dict[str, object] = {
        "python": sys.version,
        "executable": sys.executable,
        "conda_prefix": os.environ.get("CONDA_PREFIX"),
        "modules": {name: module_version(name) for name in module_names},
        "commands": {
            command: shutil.which(command)
            for command in [
                "git",
                "bsub",
                "nvidia-smi",
                "snakemake",
                "pymol",
                "obabel",
            ]
        },
        "cuda": {"torch_importable": module_version("torch") is not None},
    }
    try:
        import torch

        report["cuda"].update(
            {
                "version": torch.__version__,
                "cuda_version": torch.version.cuda,
                "available": bool(torch.cuda.is_available()),
                "device_count": torch.cuda.device_count(),
                "devices": [
                    {
                        "index": index,
                        "name": torch.cuda.get_device_name(index),
                        "capability": list(torch.cuda.get_device_capability(index)),
                    }
                    for index in range(torch.cuda.device_count())
                ],
            }
        )
    except Exception as exc:
        report["cuda"]["error"] = str(exc)

    repo = Path(carsidock["repo"])
    report["carsidock"] = {
        "repo": str(repo),
        "repo_exists": repo.is_dir(),
        "runner_exists": (repo / "run_carsidockcov_screening.py").is_file(),
        "checkpoint": file_report(Path(carsidock["checkpoint"])),
        "rtmscore_checkpoint": file_report(
            Path(carsidock["rtmscore_checkpoint"])
        ),
    }
    af3_adapter = AlphaFold3Adapter(
        af3["runner"],
        af3["python"],
        af3["model_dir"],
        af3["database_dir"],
    )
    report["alphafold3"] = af3_adapter.check_availability()

    license_value = os.environ.get(schrodinger["license_env"], "")
    license_ok, license_reason = validate_license_config(license_value)
    schrodinger_root = Path(schrodinger["root"])
    report["schrodinger"] = {
        "root": str(schrodinger_root),
        "covalent_docking_exists": (
            schrodinger_root / "covalent_docking"
        ).is_file(),
        "license_env": schrodinger["license_env"],
        "license_configured": bool(license_value),
        "license_value": "REDACTED" if license_value else None,
        "license_preflight_ok": license_ok,
        "license_preflight_reason": license_reason,
    }

    required = [
        "rdkit",
        "numpy",
        "pandas",
        "pyarrow",
        "scipy",
        "sklearn",
        "pydantic",
        "yaml",
        "openpyxl",
        "Bio",
        "pytest",
    ]
    missing = [name for name in required if report["modules"][name] is None]
    core_ready = not missing
    gpu_ready = bool(report["cuda"].get("available", False))
    carsidock_ready = bool(
        report["carsidock"]["repo_exists"]
        and report["carsidock"]["runner_exists"]
        and report["carsidock"]["checkpoint"]["exists"]
        and report["carsidock"]["rtmscore_checkpoint"]["exists"]
    )
    if not core_ready:
        status = "CORE_INCOMPLETE"
    elif args.require_gpu and not gpu_ready:
        status = "GPU_UNAVAILABLE"
    elif args.require_gpu and not carsidock_ready:
        status = "CARSIDOCK_INCOMPLETE"
    elif args.require_gpu:
        status = "GPU_READY"
    else:
        status = "CPU_READY"
    report["missing_core_modules"] = missing
    report["status"] = status

    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n")
    return 0 if status in {"CPU_READY", "GPU_READY"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
