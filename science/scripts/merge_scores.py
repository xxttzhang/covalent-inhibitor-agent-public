from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from rdkit import Chem


def _sdf_indices(path: Path) -> list[int]:
    if not path.is_file():
        return []
    out = []
    for mol in Chem.SDMolSupplier(str(path), removeHs=False, sanitize=False):
        if mol is not None and mol.HasProp("INPUT_SDF_INDEX"):
            out.append(int(mol.GetProp("INPUT_SDF_INDEX")))
    return out


def _map_chunk(chunk: pd.DataFrame, scores: pd.DataFrame, sdf_path: Path) -> tuple[pd.DataFrame, str]:
    scores = scores.copy()
    score_col = next((c for c in ["score", "raw_score", "rtmscore"] if c in scores.columns), None)
    if score_col is None:
        raise ValueError(f"No score column in {list(scores.columns)}")
    if "compound_id" in scores.columns and set(scores["compound_id"].astype(str)).issubset(set(chunk["compound_id"].astype(str))):
        mapped = scores.assign(compound_id=scores["compound_id"].astype(str))[["compound_id", score_col]].rename(columns={score_col: "raw_score"})
        method = "compound_id"
        out = chunk.copy()
        out["compound_id"] = out["compound_id"].astype(str)
        out = out.merge(mapped, on="compound_id", how="left", validate="one_to_one")
    elif "ligid" in scores.columns and set(scores["ligid"].astype(str)).issubset(set(chunk["input_sdf_index"].astype(str))):
        mapped = scores.assign(input_sdf_index=scores["ligid"].astype(int))[["input_sdf_index", score_col]].rename(columns={score_col: "raw_score"})
        method = "input_sdf_index"
        out = chunk.merge(mapped, on="input_sdf_index", how="left", validate="one_to_one")
    else:
        indices = _sdf_indices(sdf_path)
        if len(indices) != len(scores):
            raise ValueError(f"Cannot map scores safely: score_rows={len(scores)}, sdf_rows={len(indices)}")
        mapped = pd.DataFrame({"input_sdf_index": indices, "raw_score": scores[score_col].to_numpy()})
        method = "sdf_order_after_row_count_check"
        out = chunk.merge(mapped, on="input_sdf_index", how="left", validate="one_to_one")
    out["engine_status"] = out["raw_score"].notna().map({True: "SUCCESS", False: "FAILED"})
    out["score_mapping_method"] = method
    return out, method


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=Path, default=Path("data/processed/BTK/chunks"))
    ap.add_argument("--raw-dir", type=Path, default=Path("results/BTK-mini"))
    ap.add_argument("--output", type=Path, default=Path("results/BTK-mini/carsidock_rtms_merged.parquet"))
    ap.add_argument("--failures", type=Path, default=Path("results/BTK-mini/failures.csv"))
    args = ap.parse_args()

    merged = []
    failures = []
    for chunk_path in sorted(args.chunks.glob("chunk_*.parquet")):
        if chunk_path.name == "chunk_manifest.parquet":
            continue
        chunk_name = chunk_path.stem
        chunk = pd.read_parquet(chunk_path)
        raw_path = args.raw_dir / f"{chunk_name}_rtms.csv"
        if not raw_path.is_file():
            out = chunk.copy()
            out["raw_score"] = float("nan")
            out["engine_status"] = "FAILED"
            out["score_mapping_method"] = "missing_raw_score_file"
        else:
            try:
                out, method = _map_chunk(chunk, pd.read_csv(raw_path), args.chunks / f"{chunk_name}.sdf")
            except Exception as exc:
                out = chunk.copy()
                out["raw_score"] = float("nan")
                out["engine_status"] = "FAILED"
                out["score_mapping_method"] = f"mapping_error:{type(exc).__name__}"
        out["engine"] = "carsidock-cov+rtmscore"
        out["chunk"] = chunk_name
        merged.append(out)
        for _, row in out[out["engine_status"].ne("SUCCESS")].iterrows():
            failures.append({
                "compound_id": row["compound_id"],
                "engine": "carsidock-cov+rtmscore",
                "stage": "score_merge",
                "return_code": None,
                "failure_type": "missing_or_unmapped_score",
                "failure_reason": str(row["score_mapping_method"]),
                "chunk": chunk_name,
            })

    if not merged:
        raise SystemExit("No chunk parquet files found")
    result = pd.concat(merged, ignore_index=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(args.output, index=False)
    pd.DataFrame(failures).to_csv(args.failures, index=False)
    print({"rows": len(result), "success": int((result.engine_status == "SUCCESS").sum()), "failures": len(failures), "output": str(args.output)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
