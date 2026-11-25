import pandas as pd

from covscreen.dataset import build_btk_mini, build_target_mini, infer_fields


def toy_frame():
    rows = []
    for i in range(20):
        active = f"A{i:02d}"
        rows.append({"standardized_smiles": "CCO", "label": "active", "target_id": "BTK", "site_id": "C481", "compound_id": active, "parent_id": active, "protomer_id": active + "_p1", "matched_active_id": active, "source_row": i})
        for j in range(20):
            rows.append({"standardized_smiles": "CCN", "label": "decoy", "target_id": "BTK", "site_id": "C481", "compound_id": f"D{i:02d}_{j:02d}", "parent_id": f"D{i:02d}_{j:02d}", "protomer_id": f"D{i:02d}_{j:02d}_p1", "matched_active_id": active, "source_row": i * 20 + j + 1})
    return pd.DataFrame(rows)


def test_infer_fields():
    assert infer_fields(toy_frame())["smiles"] == "standardized_smiles"


def test_btk_mini_exact_420():
    out = build_btk_mini(toy_frame())
    assert len(out) == 420
    assert (out.label == "active").sum() == 20
    assert (out.label == "decoy").sum() == 400
    assert out.loc[out.label == "active", "parent_id"].nunique() == 20
    assert out["matched_active_id"].notna().all()


def test_build_target_mini_uses_declared_target_and_site():
    frame = toy_frame().copy()
    frame["target_id"] = "EGFR"
    frame["site_id"] = "EGFR"
    out = build_target_mini(
        frame,
        target_id="EGFR",
        site_id="EGFR_C797",
        active_count=2,
        decoys_per_active=2,
        seed=7,
    )
    assert len(out) == 6
    assert set(out["site_id"]) == {"EGFR_C797"}
    assert out["input_sdf_index"].tolist() == list(range(6))
