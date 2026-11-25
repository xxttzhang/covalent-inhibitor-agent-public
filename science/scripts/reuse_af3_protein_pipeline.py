from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def protein_entity(payload: dict) -> dict:
    for sequence in payload.get("sequences", []):
        if "protein" in sequence:
            return sequence["protein"]
    raise ValueError("AF3 payload has no protein entity")


def prepare(
    input_dir: Path,
    template_data_json: Path,
    output_dir: Path,
) -> dict[str, object]:
    template = json.loads(template_data_json.read_text())
    template_protein = protein_entity(template)
    required_fields = ("unpairedMsa", "pairedMsa", "templates")
    missing = [field for field in required_fields if field not in template_protein]
    if missing:
        raise ValueError(f"Template data missing protein fields: {missing}")

    output_dir.mkdir(parents=True, exist_ok=True)
    input_paths = sorted(input_dir.glob("*.json"))
    rows = []
    for input_path in input_paths:
        payload = json.loads(input_path.read_text())
        protein = protein_entity(payload)
        if protein.get("sequence") != template_protein.get("sequence"):
            raise ValueError(
                f"Protein sequence mismatch for {input_path.name}; "
                "cannot reuse MSA/templates."
            )
        for field in required_fields:
            protein[field] = template_protein[field]
        output_path = output_dir / input_path.name
        output_path.write_text(json.dumps(payload, indent=2) + "\n")
        rows.append(
            {
                "input_json": str(output_path.resolve()),
                "source_input_json": str(input_path.resolve()),
                "template_data_json": str(template_data_json.resolve()),
                "template_data_sha256": sha256_file(template_data_json),
            }
        )
    manifest = {
        "rows": len(rows),
        "input_dir": str(input_dir.resolve()),
        "output_dir": str(output_dir.resolve()),
        "template_data_json": str(template_data_json.resolve()),
        "template_data_sha256": sha256_file(template_data_json),
        "protein_sequence_length": len(template_protein["sequence"]),
        "reused_fields": list(required_fields),
        "jobs": rows,
    }
    (output_dir.parent / "protein_pipeline_reuse_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", type=Path, required=True)
    ap.add_argument("--template-data-json", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    manifest = prepare(args.input_dir, args.template_data_json, args.output_dir)
    print(json.dumps({key: value for key, value in manifest.items() if key != "jobs"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
