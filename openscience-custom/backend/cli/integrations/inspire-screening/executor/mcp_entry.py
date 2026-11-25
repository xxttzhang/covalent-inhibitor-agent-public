"""Verify an immutable release and launch its MCP against a separate data root."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validated_layout(data_root: str | Path) -> tuple[Path, Path]:
    release = Path(__file__).resolve().parent
    manifest = release / "manifest.json"
    if not manifest.is_file():
        raise ValueError("Installed source release manifest is missing")
    from code_release import verify

    verify(release, release.name, sha256(manifest))
    data = Path(data_root).expanduser()
    if not data.is_absolute() or data.is_symlink() or not data.is_dir():
        raise ValueError("MCP data root must be an existing absolute regular directory")
    data = data.resolve(strict=True)
    if data.is_relative_to(release) or release.is_relative_to(data):
        raise ValueError("MCP data root and immutable source release must be separate")
    source_config = release / "science/configs/local.inspire.yaml"
    data_config = data / "configs/local.inspire.yaml"
    if (data_config.is_symlink() or not data_config.is_file()
            or sha256(data_config) != sha256(source_config)):
        raise ValueError("MCP data-root runtime configuration differs from the source release")
    return release, data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    args = parser.parse_args(argv)
    release, data = validated_layout(args.data_root)
    source = release / "science/src"
    sys.path.insert(0, str(source))
    os.environ.update(
        COVSCREEN_PROJECT_ROOT=str(data),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
    )
    from covscreen.mcp.server import main as serve
    return serve(data)


if __name__ == "__main__":
    raise SystemExit(main())
