"""Select the frozen AF3 Top fraction without exposing activity labels."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from rdkit import Chem
from rdkit.Chem import rdDepictor

from covscreen.fusion_benchmark import deterministic_order


def read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--af3", type=Path, required=True)
    parser.add_argument("--top-fraction", type=float, default=0.05)
    parser.add_argument("--output-table", type=Path, required=True)
    parser.add_argument("--output-sdf", type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.top_fraction <= 1:
        raise ValueError("top-fraction must be in (0, 1]")
    library = read_table(args.library)
    af3 = read_table(args.af3)
    if len(library) != 420 or library["input_sdf_index"].nunique() != 420:
        raise ValueError("Frozen operational library must contain 420 unique inputs")
    if set(library.get("label", pd.Series(dtype=str)).astype(str)) & {"active", "decoy"}:
        raise ValueError("Operational library exposes activity labels")
    score = af3[["input_sdf_index", "raw_score", "engine_status", "failure_reason"]].copy()
    score = score.rename(columns={"raw_score": "af3_score", "engine_status": "af3_status", "failure_reason": "af3_failure_reason"})
    if score["input_sdf_index"].duplicated().any() or len(score) != len(library):
        raise ValueError("AF3 score table does not cover the frozen library one-to-one")
    merged = library.merge(score, on="input_sdf_index", how="left", validate="one_to_one")
    merged["af3_score"] = pd.to_numeric(merged["af3_score"], errors="coerce")
    merged.loc[~np.isfinite(merged["af3_score"]), "af3_score"] = np.nan
    merged["af3_rank"] = deterministic_order(merged, "af3_score", "lower_is_better")
    top_n = max(1, math.ceil(len(merged) * args.top_fraction))
    selected = merged.loc[merged["af3_rank"].le(top_n)].sort_values("af3_rank", kind="stable").copy()
    if len(selected) != top_n:
        raise ValueError("Frozen AF3 subset size mismatch")
    selected["ligprep_input_status"] = "PENDING"
    selected["ligprep_input_failure_reason"] = None
    args.output_table.parent.mkdir(parents=True, exist_ok=True)
    writer = Chem.SDWriter(str(args.output_sdf))
    written = 0
    for index, row in selected.iterrows():
        molecule = Chem.MolFromSmiles(str(row["standardized_smiles"]))
        if molecule is None:
            selected.loc[index, "ligprep_input_status"] = "FAILED"
            selected.loc[index, "ligprep_input_failure_reason"] = "invalid_smiles"
            continue
        rdDepictor.Compute2DCoords(molecule)
        title = f"{row['compound_id']}__p{row['protomer_id']}__i{int(row['input_sdf_index'])}"
        molecule.SetProp("_Name", title)
        molecule.SetProp("COMPOUND_ID", str(row["compound_id"]))
        molecule.SetProp("PROTOMER_ID", str(row["protomer_id"]))
        molecule.SetIntProp("INPUT_SDF_INDEX", int(row["input_sdf_index"]))
        molecule.SetIntProp("AF3_RANK", int(row["af3_rank"]))
        writer.write(molecule)
        selected.loc[index, "ligprep_input_status"] = "SUCCESS"
        written += 1
    writer.close()
    selected.to_parquet(args.output_table, index=False)
    selected.to_csv(args.output_table.with_suffix(".csv"), index=False)
    manifest = {
        "status": "SUCCESS_WITH_RETAINED_FAILURES" if written != top_n else "SUCCESS",
        "selection": f"deterministic AF3 top ceil(N*{args.top_fraction})",
        "n_input": len(merged),
        "top_n": top_n,
        "af3_success": int(merged["af3_status"].eq("SUCCESS").sum()),
        "sdf_written": written,
        "sdf_failed": top_n - written,
        "library_sha256": sha256_file(args.library),
        "af3_sha256": sha256_file(args.af3),
        "output_table_sha256": sha256_file(args.output_table),
        "output_sdf_sha256": sha256_file(args.output_sdf),
        "labels_exposed": False,
    }
    args.output_table.with_suffix(".selection_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
