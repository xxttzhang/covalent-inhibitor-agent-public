"""Pure runtime/health contract helpers with no scientific-stack imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping
from datetime import datetime, timezone


def resolve_openscience_port(environment: Mapping[str, str] | None = None) -> int:
    """Use the deployed host port; keep 4096 for legacy local deployments."""
    env = os.environ if environment is None else environment
    value = env.get("OPENSCIENCE_PORT", "4096")
    if not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise ValueError("OPENSCIENCE_PORT must be an integer between 1 and 65535")
    return int(value)


def summarize_runtime_preflight(
    *,
    probe: Mapping[str, Any],
    engines: Mapping[str, Any],
    executable: str | Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Separate probe success from availability of optional CUDA resources."""
    env = os.environ if environment is None else environment
    cuda = probe.get('cuda')
    probe_passed = probe.get("status") == "PASS"
    cuda_confirmed = (probe_passed and isinstance(cuda, Mapping)
        and type(cuda.get('available')) is bool
        and type(cuda.get('device_count')) is int
        and cuda['device_count'] >= 0
        and cuda['available'] == (cuda['device_count'] > 0))
    if not cuda_confirmed:
        cuda = {"available": None, "device_count": None, "devices": []}
    cuda_available = cuda['available']
    limitations = []
    if not probe_passed:
        limitations.append("runtime_unconfirmed_probe_failed_stop_required")
    if probe_passed and not cuda_confirmed:
        limitations.append('cuda_unconfirmed_missing_or_inconsistent_probe_evidence')
    if cuda_confirmed and not cuda_available:
        limitations.append("cuda_unavailable_gpu_tools_blocked_cpu_tools_may_run")
    return {
        "status": "PASS" if probe_passed else "FAIL",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "observation_scope": "Point-in-time probe of the named Python runtime; not a scheduler allocation or reservation.",
        "resource_budget_fit": "NOT_VERIFIED",
        "resource_reservation": "NONE",
        "python_executable": os.path.realpath(executable),
        "versions": probe.get("versions", {}),
        "cuda": cuda,
        "resource_classes": {
            "cpu": {
                "status": "AVAILABLE" if probe_passed else "UNKNOWN",
                "available": True if probe_passed else None,
            },
            "cuda": {
                "status": ("AVAILABLE" if cuda_available else "UNAVAILABLE") if cuda_confirmed else "UNKNOWN",
                "available": cuda_available,
                "device_count": cuda['device_count'],
            },
        },
        "limitations": limitations,
        "decision_rule": (
            "Match the next tool's declared resource_budget to resource_classes. "
            "Missing CUDA blocks only tools whose exact plan requires gpu_count > 0. "
            "CPU/CUDA availability alone does not verify requested CPU count, memory, GPU memory or scheduler quota. "
            "Do not report the whole resource budget as ready or reserved from this probe."
        ),
        "probe_error": probe.get("error"),
        "probe_error_kind": probe.get("error_kind"),
        "probe_timeout_seconds": probe.get("timeout_seconds"),
        "probe_elapsed_seconds": probe.get("elapsed_seconds"),
        "probe_stage": probe.get("stage"),
        "probe_timings_seconds": probe.get("timings_seconds", {}),
        "engines": dict(engines),
        "safety": {
            "ld_preload_present": bool(env.get("LD_PRELOAD")),
            "schrodinger_license_inherited": bool(env.get("SCHROD_LICENSE_FILE")),
            "expensive_execution_enabled": False,
        },
    }


def resolve_openscience_config(
    root: str | Path,
    *,
    cwd: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> tuple[Path | None, list[str]]:
    """Locate a parseable config across separated code, data and runtime roots."""
    root = Path(root)
    env = os.environ if environment is None else environment
    current = Path.cwd() if cwd is None else Path(cwd)
    user_home = Path.home() if home is None else Path(home)
    candidates: list[Path] = []
    explicit = env.get("OPENSCIENCE_CONFIG")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend((root / "openscience.json", current / "openscience.json"))
    config_home = Path(
        env.get("XDG_CONFIG_HOME", str(user_home / ".config"))
    ).expanduser()
    candidates.append(config_home / "openscience" / "openscience.json")
    checked: list[str] = []
    seen: set[Path] = set()
    for candidate in candidates:
        absolute = candidate.absolute()
        if absolute in seen:
            continue
        seen.add(absolute)
        checked.append(str(absolute))
        if absolute.is_symlink() or not absolute.is_file():
            continue
        try:
            parsed = json.loads(absolute.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return absolute, checked
    return None, checked
