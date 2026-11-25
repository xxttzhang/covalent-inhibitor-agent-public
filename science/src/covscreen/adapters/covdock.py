from __future__ import annotations

import os
from pathlib import Path

from .base import EngineAdapter
from covscreen.schrodinger import validate_license_config


class CovDockAdapter(EngineAdapter):
    name = "schrodinger-covdock-vs"

    def __init__(
        self,
        root: str,
        license_env: str = "SCHROD_LICENSE_FILE",
    ):
        self.root = Path(root)
        self.license_env = license_env

    def check_availability(self) -> dict[str, object]:
        executable = self.root / "covalent_docking"
        license_value = os.environ.get(self.license_env, "").strip()
        unsafe_marker = any(
            marker in license_value.lower() for marker in ("crack", "patcher")
        )
        license_ok, license_reason = validate_license_config(license_value)
        report = {
            "root_exists": self.root.is_dir(),
            "executable_exists": executable.is_file(),
            "license_env": self.license_env,
            "license_configured": bool(license_value),
            "unsafe_license_marker": unsafe_marker,
            "license_preflight_reason": license_reason,
            # Do not print a license path/server value into manifests.
            "license_value_redacted": bool(license_value),
        }
        report["ready"] = bool(
            report["root_exists"]
            and report["executable_exists"]
            and report["license_configured"]
            and license_ok
        )
        if not license_ok:
            report["reason"] = (
                "A legitimate Schrödinger license file or license server must "
                f"be configured through {self.license_env}; bypass/patcher "
                "artifacts are not accepted."
            )
        return report

    def build_command(
        self,
        receptor_mae: str,
        ligand_mae: str,
        attachment_residue: str,
        jobname: str,
        reaction_type: str = "Michael Addition",
        mode: str = "enrichment",
        njobs: int = 1,
        grid_file: str | None = None,
        extra_options: list[str] | None = None,
        wait: bool = True,
    ) -> list[str]:
        """Build the command documented by CovDock 2021-2 ``-h``."""
        if mode not in {"leadopt", "enrichment", "score", "attach"}:
            raise ValueError(f"Unsupported CovDock mode: {mode}")
        command = [
            str(self.root / "covalent_docking"),
            str(receptor_mae),
            str(ligand_mae),
            attachment_residue,
            "-rxn_type",
            reaction_type,
            "-mode",
            mode,
            "-jobname",
            jobname,
            "-NJOBS",
            str(njobs),
        ]
        if grid_file:
            command.extend(["-grid_file", str(grid_file)])
        if extra_options:
            command.extend(extra_options)
        if wait:
            command.append("-WAIT")
        return command

    def parse_output(self, output_dir: str | Path) -> dict[str, object]:
        output_dir = Path(output_dir)
        all_maestro = [
            p
            for pattern in ("*.mae", "*.maegz")
            for p in sorted(output_dir.rglob(pattern))
        ]
        pose_files = [
            p
            for p in all_maestro
            if (
                "-out." in p.name
                or "_out." in p.name
                or "_pv." in p.name
                or "pose" in p.name.lower()
            )
            and "protassign" not in p.name.lower()
            and "impref" not in p.name.lower()
        ]
        logs = [p for p in sorted(output_dir.rglob("*.log"))]
        all_entries_failed = any(
            "All Entries Failed" in path.read_text(errors="replace")
            for path in logs
        )
        return {
            "maegz": [str(p) for p in sorted(output_dir.rglob("*.maegz"))],
            "mae": [str(p) for p in sorted(output_dir.rglob("*.mae"))],
            "pose_files": [str(p) for p in pose_files],
            "csv": [str(p) for p in sorted(output_dir.rglob("*.csv"))],
            "logs": [str(p) for p in logs],
            "all_entries_failed": all_entries_failed,
        }

    def validate_output(self, parsed: dict[str, object]) -> bool:
        return bool(
            parsed["pose_files"] and not parsed["all_entries_failed"]
        )
