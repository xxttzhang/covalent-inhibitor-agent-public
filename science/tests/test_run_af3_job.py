import importlib.util
import sys
import sysconfig
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_af3_job.py"
SPEC = importlib.util.spec_from_file_location("run_af3_job", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_af3_site_packages_resolves_compiled_environment():
    assert MODULE.af3_site_packages(Path(sys.executable)) == Path(sysconfig.get_path("platlib"))


def test_hash_parsed_outputs_covers_every_existing_file(tmp_path: Path):
    model = tmp_path / "model.cif"
    confidence = tmp_path / "summary_confidences.json"
    model.write_text("model")
    confidence.write_text("{}")

    hashes = MODULE.hash_parsed_outputs(
        {"models": [str(model)], "summary_confidences": [str(confidence)]}
    )

    assert set(hashes["models"]) == {str(model)}
    assert set(hashes["summary_confidences"]) == {str(confidence)}
    assert len(hashes["models"][str(model)]) == 64
