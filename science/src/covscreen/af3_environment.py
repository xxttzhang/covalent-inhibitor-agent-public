"""Isolate native AF3 subprocesses from the Python 3.11 orchestration runtime."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping
import os
import subprocess


def af3_site_packages(python: Path) -> Path:
    # Old package directories can remain after an interpreter upgrade. Ask the
    # selected interpreter, in isolated mode, instead of guessing by glob order.
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONHOME", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH"}}
    environment["LD_LIBRARY_PATH"] = f"{python.parents[1]}/lib"
    try:
        result = subprocess.run([str(python), "-I", "-c",
            "import sysconfig; print(sysconfig.get_path('platlib'))"],
            env=environment, text=True, capture_output=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"Cannot query native AF3 package directory: {python}") from error
    location = Path(result.stdout.strip())
    if not location.is_absolute() or not location.is_dir():
        raise ValueError(f"Native AF3 package directory is unavailable: {python}")
    return location


def af3_environment(python: Path, source: Path, inherited: Mapping[str, str]) -> dict[str, str]:
    # Native dependencies must come only from the selected interpreter's prefix.
    # Keep covscreen source available without importing the orchestrator's binary packages.
    environment = {key: value for key, value in inherited.items() if key not in {
        "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "LD_PRELOAD", "LD_LIBRARY_PATH",
        "SCHRODINGER", "SCHRODINGER_LICENSE_FILE", "SCHROD_LICENSE_FILE", "SCHRODINGER_PYTHONPATH",
    }}
    environment.update({
        "PYTHONPATH": f"{af3_site_packages(python)}:{source}",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
        "PATH": f"{python.parent}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LD_LIBRARY_PATH": f"{python.parents[1]}/lib:/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu",
    })
    return environment
