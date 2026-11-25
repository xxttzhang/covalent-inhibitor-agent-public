from covscreen.models import (
    CandidatePassport,
    CandidateRecord,
    FailureRecord,
    ModelResult,
)


def test_candidate_record():
    x = CandidateRecord(compound_id="c1", parent_id="p1", protomer_id="pr1", target_id="BTK", site_id="C481", label="active", standardized_smiles="CCO")
    assert x.label == "active"


def test_model_result_and_failure():
    assert ModelResult(compound_id="c1", engine="carsidock", status="FAILED").status == "FAILED"
    assert FailureRecord(compound_id="c1", engine="carsidock", stage="dock", failure_type="x", failure_reason="y").failure_type == "x"


def test_candidate_passport_defaults():
    passport = CandidatePassport(
        compound_id="c1",
        target_id="BTK",
        site_id="BTK_C481",
    )
    assert passport.failure_flags == []
    assert passport.consensus_score is None
