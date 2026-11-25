from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pandas as pd


class DatasetError(RuntimeError):
    pass


ALIASES = {
    "smiles": ["smiles", "canonical_smiles", "standardized_smiles", "isomeric_smiles", "protomer_smiles"],
    "label": ["label", "class", "activity", "active_decoy", "active_or_decoy"],
    "target_id": ["target", "target_id", "protein", "protein_id"],
    "site_id": ["site", "site_id", "covalent_site", "attachment_site"],
    "compound_id": ["compound_id", "compound", "molecule_id", "ligand_id", "id"],
    "parent_id": ["parent_id", "parent", "parent_compound_id", "parent_compound"],
    "protomer_id": ["protomer_id", "protomer", "protonation_id"],
    "matched_active_id": ["matched_active_id", "active_id", "matched_active", "matched_id"],
}


def _official_covalid_fields(frame: pd.DataFrame, sheet: str) -> dict[str, str | None] | None:
    """Recognize the published COValid S3 active/decoy worksheet schema."""
    columns = {canonical_column(c): str(c) for c in frame.columns}
    suffix = sheet.lower().rsplit("_", 1)[-1]
    required = {"protomer_smiles", "active_compound_name", "active_protomer_ind"}
    if suffix not in {"actives", "decoys"} or not required.issubset(columns):
        return None
    if suffix == "actives" and not ("mol_chembl_id" in columns or "mol_bindingdb_id" in columns):
        return None
    if suffix == "decoys" and not ("decoy_zinc_id" in columns and "matched_decoy_protomer" in columns):
        return None
    target = sheet.rsplit("_", 1)[0]
    return {
        "smiles": columns["protomer_smiles"],
        "label": "__active__" if suffix == "actives" else "__decoy__",
        "target_id": target,
        # S3 carries the benchmark panel key in the worksheet name but does not
        # repeat the residue number.  Use one shared site key for the active and
        # decoy worksheet pair; the residue-specific mapping is supplied by the
        # target configuration when a receptor is prepared.
        "site_id": target,
        "compound_id": columns["active_compound_name"] if suffix == "actives" else columns["decoy_zinc_id"],
        "parent_id": columns["active_compound_name"],
        "protomer_id": columns["active_protomer_ind"],
        "matched_active_id": columns["active_compound_name"],
        "source_id": (columns.get("mol_chembl_id") or columns.get("mol_bindingdb_id")) if suffix == "actives" else columns["decoy_zinc_id"],
        "source_protomer_id": columns.get("protomer_ind"),
        "source_sheet": sheet,
    }


def canonical_column(name: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def workbook_sheets(path: str | Path) -> list[str]:
    return pd.ExcelFile(path).sheet_names


def load_workbook(path: str | Path) -> dict[str, pd.DataFrame]:
    xls = pd.ExcelFile(path)
    return {sheet: pd.read_excel(xls, sheet_name=sheet) for sheet in xls.sheet_names}


def _find_column(columns: Iterable[object], aliases: list[str]) -> str | None:
    normalized = {canonical_column(c): str(c) for c in columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    for key, original in normalized.items():
        if any(alias in key for alias in aliases):
            return original
    return None


def infer_fields(frame: pd.DataFrame) -> dict[str, str | None]:
    return {field: _find_column(frame.columns, aliases) for field, aliases in ALIASES.items()}


def choose_protomer_sheet(sheets: dict[str, pd.DataFrame]) -> tuple[str, pd.DataFrame, dict[str, str | None]]:
    candidates = []
    for name, frame in sheets.items():
        fields = infer_fields(frame)
        score = int(fields["smiles"] is not None) + int(fields["label"] is not None)
        candidates.append((score, len(frame), name, frame, fields))
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if not candidates or candidates[0][0] < 2:
        raise DatasetError("Could not identify a protomer sheet with SMILES and active/decoy fields")
    _, _, name, frame, fields = candidates[0]
    return name, frame.copy(), fields


def _label(value: object) -> str | None:
    text = str(value).strip().lower()
    if text in {"active", "1", "true", "yes", "positive"}:
        return "active"
    if text in {"decoy", "0", "false", "no", "negative", "inactive"}:
        return "decoy"
    return None


def normalize_protomers(path: str | Path) -> tuple[pd.DataFrame, dict[str, object]]:
    sheets = load_workbook(path)
    official = [(name, frame, _official_covalid_fields(frame, name)) for name, frame in sheets.items()]
    official = [(name, frame, fields) for name, frame, fields in official if fields is not None]
    if official:
        records = []
        mappings = {}
        for name, raw, fields in official:
            mappings[name] = fields
            is_active = fields["label"] == "__active__"
            for source_row, row in raw.iterrows():
                source_id = row[fields["source_id"]] if fields["source_id"] else ""
                if pd.isna(source_id) or str(source_id).strip() in {"", "-"}:
                    source_id = row[fields["compound_id"]]
                records.append({
                    "standardized_smiles": str(row[fields["smiles"]]).strip(),
                    "label": "active" if is_active else "decoy",
                    "target_id": fields["target_id"],
                    "site_id": fields["site_id"],
                    "compound_id": str(row[fields["compound_id"]]).strip(),
                    "parent_id": str(row[fields["parent_id"]]).strip(),
                    "protomer_id": str(row[fields["protomer_id"]]).strip(),
                    "matched_active_id": str(row[fields["matched_active_id"]]).strip(),
                    "source_id": str(source_id).strip(),
                    "source_protomer_id": str(row[fields["source_protomer_id"]]).strip() if fields["source_protomer_id"] else "",
                    "source_row": int(source_row),
                    "source_sheet": name,
                })
        out = pd.DataFrame.from_records(records)
        for col in ["standardized_smiles", "target_id", "site_id", "compound_id", "parent_id", "protomer_id", "matched_active_id", "source_id", "source_protomer_id"]:
            out[col] = out[col].replace({"nan": "", "None": ""})
        report = {
            "format": "COValid S3 published active/decoy worksheets",
            "sheets": {k: {"rows": len(v), "columns": [str(c) for c in v.columns]} for k, v in sheets.items()},
            "selected_sheets": [name for name, _, _ in official],
            "field_mapping": mappings,
        }
        return out.reset_index(drop=True), report

    sheet, raw, fields = choose_protomer_sheet(sheets)
    required = ["smiles", "label", "target_id", "compound_id", "protomer_id", "matched_active_id"]
    missing = [f for f in required if fields.get(f) is None and f not in {"target_id", "matched_active_id"}]
    if missing:
        raise DatasetError(f"Missing required fields: {missing}; inferred={fields}")

    out = pd.DataFrame(index=raw.index)
    out["standardized_smiles"] = raw[fields["smiles"]].astype(str).str.strip()
    out["label"] = raw[fields["label"]].map(_label)
    for field in ["target_id", "site_id", "compound_id", "parent_id", "protomer_id", "matched_active_id"]:
        source = fields.get(field)
        out[field] = raw[source].astype(str).str.strip() if source else ""
    out["source_row"] = raw.index.astype(int)
    out["source_sheet"] = sheet
    out = out[out["label"].notna()].copy()
    out["target_id"] = out["target_id"].replace({"nan": ""})
    out["site_id"] = out["site_id"].replace({"nan": ""})
    out["compound_id"] = out["compound_id"].replace({"nan": ""})
    out["parent_id"] = out["parent_id"].replace({"nan": ""})
    out["protomer_id"] = out["protomer_id"].replace({"nan": ""})
    out["matched_active_id"] = out["matched_active_id"].replace({"nan": ""})
    out["source_id"] = out["compound_id"]
    out["source_protomer_id"] = out["protomer_id"]
    for field in ["compound_id", "parent_id", "protomer_id"]:
        empty = out[field].eq("")
        out.loc[empty, field] = out.loc[empty, "source_row"].map(lambda x: f"row_{x}")
    report = {"sheets": {k: {"rows": len(v), "columns": [str(c) for c in v.columns]} for k, v in sheets.items()}, "selected_sheet": sheet, "field_mapping": fields}
    return out.reset_index(drop=True), report


def select_btk_rows(frame: pd.DataFrame, target_regex: str = r"(?i)BTK", site_regex: str = r"(?i)C481|BTK") -> pd.DataFrame:
    target_text = frame["target_id"].fillna("") + " " + frame["site_id"].fillna("")
    mask = target_text.str.contains(target_regex, regex=True, na=False)
    if not mask.any():
        raise DatasetError("No BTK rows found using target/site fields")
    selected = frame.loc[mask].copy()
    site_mask = (selected["site_id"].eq("") | selected["site_id"].str.contains(site_regex, regex=True, na=False))
    return selected.loc[site_mask].copy()


def build_btk_mini(frame: pd.DataFrame, active_count: int = 20, decoys_per_active: int = 20, seed: int = 20260725) -> pd.DataFrame:
    btk = select_btk_rows(frame)
    return build_target_mini(
        btk,
        target_id="BTK",
        site_id="BTK_C481",
        active_count=active_count,
        decoys_per_active=decoys_per_active,
        seed=seed,
    )


def build_target_mini(
    frame: pd.DataFrame,
    *,
    target_id: str,
    site_id: str,
    active_count: int = 20,
    decoys_per_active: int = 20,
    seed: int = 20260725,
) -> pd.DataFrame:
    """Build a deterministic matched active/decoy subset for one COValid target."""
    selected = frame.loc[frame["target_id"].astype(str).eq(target_id)].copy()
    if selected.empty:
        raise DatasetError(f"No rows found for target_id={target_id}")
    active = selected[selected["label"].eq("active")].sort_values(
        ["parent_id", "protomer_id", "source_row"]
    )
    active = active.drop_duplicates("parent_id", keep="first").head(active_count)
    if len(active) != active_count:
        raise DatasetError(
            f"{target_id} has only {len(active)} distinct active parents; "
            f"need {active_count}"
        )

    parts = []
    for _, row in active.iterrows():
        active_id = str(row["compound_id"])
        parent = str(row["parent_id"])
        decoys = selected[selected["label"].eq("decoy")]
        match = decoys[decoys["matched_active_id"].isin({active_id, parent, str(row["protomer_id"] )})]
        if len(match) < decoys_per_active:
            raise DatasetError(f"Only {len(match)} matched decoys for active {active_id}; need {decoys_per_active}")
        chosen = match.sort_values(["protomer_id", "source_row"]).head(decoys_per_active).copy()
        active_copy = row.to_frame().T.copy()
        active_copy["matched_active_id"] = active_id
        parts.append(active_copy)
        chosen["matched_active_id"] = active_id
        parts.append(chosen)
    result = pd.concat(parts, ignore_index=True)
    result["compound_id"] = result["compound_id"].astype(str)
    result["parent_id"] = result["parent_id"].astype(str)
    result["protomer_id"] = result["protomer_id"].astype(str)
    result["label"] = pd.Categorical(result["label"], categories=["active", "decoy"])
    result = result.sample(frac=1.0, random_state=seed).reset_index(drop=True)
    result["input_sdf_index"] = range(len(result))
    result["site_id"] = site_id
    return result
