import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "reuse_af3_protein_pipeline.py"
spec = importlib.util.spec_from_file_location("reuse_af3_protein_pipeline", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_reuse_af3_pipeline_preserves_ligand_and_replaces_protein_cache(tmp_path):
    input_dir = tmp_path / "inputs"
    output_dir = tmp_path / "precomputed"
    input_dir.mkdir()
    template = {
        "sequences": [
            {
                "protein": {
                    "id": "A",
                    "sequence": "ACD",
                    "unpairedMsa": "msa-a",
                    "pairedMsa": "msa-b",
                    "templates": [{"mmcif": "x"}],
                }
            }
        ]
    }
    candidate = {
        "name": "job",
        "sequences": [
            {
                "protein": {
                    "id": "A",
                    "sequence": "ACD",
                    "unpairedMsa": "old",
                    "pairedMsa": "old",
                    "templates": [],
                }
            },
            {"ligand": {"id": "L", "ccdCodes": ["LIG-X"]}},
        ],
        "userCCD": "ccd",
        "bondedAtomPairs": [["A", 1, "SG"]],
    }
    template_path = tmp_path / "template.json"
    input_path = input_dir / "job.json"
    template_path.write_text(json.dumps(template))
    input_path.write_text(json.dumps(candidate))

    manifest = module.prepare(input_dir, template_path, output_dir)
    assert manifest["rows"] == 1
    result = json.loads((output_dir / "job.json").read_text())
    protein = result["sequences"][0]["protein"]
    assert protein["unpairedMsa"] == "msa-a"
    assert protein["pairedMsa"] == "msa-b"
    assert result["sequences"][1]["ligand"]["id"] == "L"
    assert result["bondedAtomPairs"] == candidate["bondedAtomPairs"]
