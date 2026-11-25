from __future__ import annotations

import hashlib
from pathlib import Path


UNSAFE_LICENSE_MARKERS = ("crack", "patcher", "keygen")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_license_config(value: str) -> tuple[bool, str]:
    """Apply conservative checks before invoking licensed software.

    This does not prove entitlement; the vendor must still check the license.
    Unused neighboring files are not evidence about the configured license.
    """
    value = value.strip()
    if not value:
        return False, "SCHROD_LICENSE_FILE is not configured"
    lower = value.lower()
    if any(marker in lower for marker in UNSAFE_LICENSE_MARKERS):
        return False, "license configuration contains an unsafe marker"
    return True, "configured"
