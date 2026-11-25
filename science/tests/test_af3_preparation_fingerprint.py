import importlib.util
from pathlib import Path


path = Path(__file__).resolve().parents[1] / "scripts/prepare_af3_covalent_batch.py"
spec = importlib.util.spec_from_file_location("af3_batch_fingerprint_test", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_fingerprint_covers_candidate_and_protocol():
    fingerprint = module.preparation_fingerprint
    row = {"compound_id": "a", "warhead_class": "acrylamide", "standardized_smiles": "C=CC(=O)N"}
    context = {"source_sha256": "source", "adduct_sha256": "v1", "cysteine_sequence_index": 88}
    original = fingerprint(row, context)
    assert original == fingerprint(dict(reversed(list(row.items()))), context)
    assert original != fingerprint({**row, "warhead_class": "chloroacetamide"}, context)
    assert original != fingerprint(row, {**context, "adduct_sha256": "v2"})
    assert original != fingerprint(row, {**context, "cysteine_sequence_index": 89})
