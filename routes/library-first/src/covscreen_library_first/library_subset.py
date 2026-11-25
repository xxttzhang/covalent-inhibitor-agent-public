#!/usr/bin/env python3
"""Freeze an exact CSV prefix and row identities without screening molecules."""
import csv
import hashlib
import io
import json
import os

# Exact pre-migration implementation; existing frozen outputs retain their provenance.
LEGACY_FREEZER = "85ab43940ddbd2be27161ad322e3d227641ce83291d300747306f5e9aee98a23"
from pathlib import Path


def sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def freeze(source, output, records, expected_sha256):
    source = Path(source).resolve(strict=True)
    output = Path(output)
    if records != 20000:
        raise ValueError("This pilot freezes exactly 20,000 source records")
    with source.open("rb") as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("Source library differs from the expected full-file hash")
        stream.seek(0)
        wrapper = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
        reader = csv.reader(wrapper)
        header = next(reader)
        if len(set(header)) != len(header) or not header:
            raise ValueError("Library header is missing or ambiguous")
        rows = []
        for index in range(records):
            row = next(reader, None)
            if row is None or len(row) != len(header):
                raise ValueError("Library prefix is incomplete or has inconsistent columns")
            rows.append(row)
        after = os.fstat(stream.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ValueError("Source library changed while freezing")
    normalized = io.StringIO(newline="")
    writer = csv.writer(normalized, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    subset = normalized.getvalue().encode("utf-8")
    identities = [{"source_record_index": index, "row_sha256": hashlib.sha256(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()}
        for index, row in enumerate(rows)]
    manifest = {
        "schema": "drug-design-frozen-library-prefix-v1",
        "operation": "data-freeze-only-no-molecule-screening",
        "source": {"path": str(source), "sha256": expected_sha256, "size_bytes": before.st_size},
        "selection": {"method": "file_order_prefix", "source_record_index_base": 0,
                      "records": records, "unique_row_hashes": len({row["row_sha256"] for row in identities}),
                      "duplicates_preserved": True},
        "subset": {"filename": "library-prefix.csv", "sha256": hashlib.sha256(subset).hexdigest(),
                   "size_bytes": len(subset), "encoding": "utf-8", "line_ending": "LF",
                   "semantics": "CSV rows preserved; serialization normalized; original source hash remains mandatory"},
        "header": header, "records": identities,
        "freezer_sha256": sha(__file__),
    }
    encoded = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if output.exists():
        if output.is_symlink() or (output / "manifest.json").is_symlink() or (output / "library-prefix.csv").is_symlink():
            raise ValueError("Frozen input cannot use symlinks")
        saved = json.loads((output / "manifest.json").read_text())
        previous = saved.get("freezer_sha256")
        expected = {**manifest, "freezer_sha256": previous}
        if previous not in {manifest["freezer_sha256"], LEGACY_FREEZER} or saved != expected or (output / "library-prefix.csv").read_bytes() != subset:
            raise ValueError("Existing frozen output differs; refusing overwrite")
        encoded = (output / "manifest.json").read_bytes()
        resumed = True
    else:
        output.mkdir(parents=True, exist_ok=False)
        with (output / "library-prefix.csv").open("xb") as stream:
            stream.write(subset)
        with (output / "manifest.json").open("xb") as stream:
            stream.write(encoded)
        resumed = False
    return {"manifest": str(output / "manifest.json"), "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
            "source_sha256": expected_sha256, "subset_sha256": manifest["subset"]["sha256"],
            "records": records, "resumed": resumed, "new_scientific_computation": False}

