"""Exercise the real ranking command; fixtures are not activity labels."""
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest


@pytest.mark.parametrize("policy", ["legacy", "rrf-finite-complete-case-v2"])
def test_ranking_cli_preserves_candidates_and_records_selected_policy(tmp_path, policy):
    library = pd.DataFrame({"compound_id": ["complete", "partial", "absent"],
        "protomer_id": ["p1", "p2", "p3"], "input_sdf_index": [0, 1, 2]})
    source = tmp_path / "library.parquet"
    library.to_parquet(source, index=False)
    for name, scores in (("a", [1., np.inf, np.nan]), ("b", [2., 3., np.nan])):
        library.assign(score=scores).to_parquet(tmp_path / (name + ".parquet"), index=False)
    script = Path(__file__).resolve().parents[1] / "scripts/fuse_rankings.py"
    output = tmp_path / "ranked.parquet"
    result = subprocess.run([sys.executable, str(script), "--library", str(source),
        "--engine", f"a:{tmp_path / 'a.parquet'}:score:lower_is_better",
        "--engine", f"b:{tmp_path / 'b.parquet'}:score:lower_is_better",
        "--policy", policy, "--output", str(output)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert policy in result.stdout
    actual = pd.read_parquet(output)
    assert actual.compound_id.tolist() == library.compound_id.tolist()
    assert output.with_suffix(".csv").is_file()
    if policy != "legacy":
        assert actual.consensus_policy.eq(policy).all()
        assert actual.evidence_coverage.tolist() == ["COMPLETE", "PARTIAL", "NONE"]
        assert actual.consensus_rank.iloc[1:].isna().all()
        assert actual.consensus_score.iloc[1:].isna().all()
    else:
        assert actual.consensus_score.notna().all()
