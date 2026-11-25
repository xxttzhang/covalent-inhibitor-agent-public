from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from .base import EngineAdapter


class CarsiDockAdapter(EngineAdapter):
    name = "carsidock-cov"

    def __init__(self, repo: str, checkpoint: str, device: str = "cuda"):
        self.repo = Path(repo)
        self.checkpoint = Path(checkpoint)
        self.device = device

    def check_availability(self) -> dict[str, object]:
        torch_ok = importlib.util.find_spec("torch") is not None
        return {
            "repo_exists": self.repo.is_dir(),
            "checkpoint_exists": self.checkpoint.is_file(),
            "torch_importable": torch_ok,
            "ready": self.repo.is_dir() and self.checkpoint.is_file() and torch_ok,
            "checkpoint_sha256": hashlib.sha256(self.checkpoint.read_bytes()).hexdigest() if self.checkpoint.is_file() else None,
        }

    def build_command(
        self,
        pdb: str,
        reference_ligand: str,
        ligand_sdf: str,
        covres: str,
        output: str,
        device: str | None = None,
        num_conformer: int = 1,
        cuda_device_index: int = 0,
        seed: int = 20260725,
        reaction_type: str = "Michael Addition",
    ) -> list[str]:
        return [
            "python", str(self.repo / "run_carsidockcov_screening.py"),
            "-p", pdb, "-rl", reference_ligand, "-l", ligand_sdf,
            "-covres", covres, "-rectype", reaction_type,
            "-remove_dummyatom", "-remain_pocket",
            "--ckpt_path", str(self.checkpoint),
            "--num_conformer", str(num_conformer), "--seed", str(seed),
            "--device", device or self.device,
            "--cuda_device_index", str(cuda_device_index),
            "-o", output,
        ]

    def parse_output(self, output_dir: str | Path):
        output_dir = Path(output_dir)
        csvs = sorted(output_dir.glob("*.csv"))
        return csvs
