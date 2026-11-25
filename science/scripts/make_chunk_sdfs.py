from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from rdkit import Chem


def main() -> int:
    ap = argparse.ArgumentParser(description="Make one SDF per BTK-mini chunk while preserving input indices.")
    ap.add_argument("--table", type=Path, default=Path("data/processed/BTK/btk_mini.parquet"))
    ap.add_argument("--sdf", type=Path, default=Path("data/processed/BTK/btk_mini.sdf"))
    ap.add_argument("--chunks", type=Path, default=Path("data/processed/BTK/chunks"))
    args = ap.parse_args()

    table = pd.read_parquet(args.table)
    del table  # The chunk tables are authoritative for membership below.
    supplier = Chem.SDMolSupplier(str(args.sdf), removeHs=False, sanitize=True)
    molecules = {}
    for mol in supplier:
        if mol is None or not mol.HasProp("INPUT_SDF_INDEX"):
            continue
        molecules[int(mol.GetProp("INPUT_SDF_INDEX"))] = mol

    manifest_rows = []
    for chunk_path in sorted(args.chunks.glob("chunk_*.parquet")):
        if chunk_path.name == "chunk_manifest.parquet":
            continue
        chunk = pd.read_parquet(chunk_path)
        out_path = chunk_path.with_suffix(".sdf")
        writer = Chem.SDWriter(str(out_path))
        written = []
        missing = []
        for _, row in chunk.iterrows():
            idx = int(row["input_sdf_index"])
            mol = molecules.get(idx)
            if mol is None:
                missing.append(idx)
                continue
            writer.write(mol)
            written.append(idx)
        writer.close()
        manifest_rows.append({
            "chunk": chunk_path.stem,
            "table_rows": len(chunk),
            "sdf_rows": len(written),
            "missing_rows": len(missing),
            "missing_input_sdf_indices": ",".join(map(str, missing)),
            "sdf": str(out_path),
        })

    pd.DataFrame(manifest_rows).to_json(args.chunks / "chunk_sdf_manifest.json", orient="records", indent=2)
    print(pd.DataFrame(manifest_rows).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
