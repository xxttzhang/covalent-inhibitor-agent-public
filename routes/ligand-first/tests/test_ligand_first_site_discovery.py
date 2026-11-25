from pathlib import Path

from covscreen.ligand_first_inputs import load_campaign
from covscreen.ligand_first_site_discovery import (
    discover_covalentizer_sites,
    exit_vector_specs,
)
from covscreen.rdkit_covalentizer import FORMAL_COVALENTIZER_TRANSFORMATIONS


ROOT = (Path(__file__).resolve().parents[3] / "science")
CAMPAIGN_PATH = ROOT / "projects/egfr-c797/ligand_first/campaign.json"


def test_rule_discovery_recovers_primary_exit_and_protects_negative_control():
    campaign = load_campaign(CAMPAIGN_PATH)
    result = discover_covalentizer_sites(
        campaign,
        CAMPAIGN_PATH.parent,
        max_sites=50,
    )

    assert result.status == "PASS"
    by_id = {candidate.exit_vector_id: candidate for candidate in result.candidates}
    assert by_id["auto-C9-O1"].advance is True
    assert by_id["auto-C13-O3"].advance is False
    assert any(
        "explicitly protected" in reason
        for reason in by_id["auto-C13-O3"].rejection_reasons
    )

    specs = {item.exit_vector_id: item for item in exit_vector_specs(result)}
    assert specs["auto-C9-O1"].allowed_transformations == list(
        FORMAL_COVALENTIZER_TRANSFORMATIONS
    )
    assert specs["auto-C13-O3"].allowed_transformations == []

