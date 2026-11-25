from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CandidateRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    compound_id: str
    parent_id: str
    protomer_id: str
    target_id: str
    site_id: str
    label: Literal["active", "decoy"]
    matched_active_id: str | None = None
    standardized_smiles: str
    source: str = "COValid"
    input_sdf_index: int | None = None
    chain_id: str | None = None
    residue_name: str | None = None
    residue_number: int | None = None
    warhead_class: str | None = None
    electrophilic_atom: str | None = None
    reaction_type: str | None = None


class ModelResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    compound_id: str
    engine: str
    status: Literal["SUCCESS", "FAILED", "UNAVAILABLE"]
    raw_score: float | None = None
    score_direction: Literal["higher_is_better", "lower_is_better"] | None = None
    rank: int | None = None
    percentile: float | None = None
    pose_file: str | None = None
    runtime_seconds: float | None = None
    failure_reason: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class FailureRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    compound_id: str
    engine: str
    stage: str
    return_code: int | None = None
    failure_type: str
    failure_reason: str
    stdout_file: str | None = None
    stderr_file: str | None = None


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="allow")
    run_id: str
    engine: str
    target_id: str
    site_id: str
    input_count: int
    command: list[str] = Field(default_factory=list)
    status: str
    score_direction: str = "PENDING_CONFIRMATION"


class CandidatePassport(BaseModel):
    model_config = ConfigDict(extra="allow")
    compound_id: str
    target_id: str
    site_id: str
    warhead_class: str | None = None
    reaction_type: str | None = None
    carsidock_score: float | None = None
    carsidock_rank: int | None = None
    af3_mpae: float | None = None
    af3_rank: int | None = None
    covdock_score: float | None = None
    covdock_rank: int | None = None
    consensus_score: float | None = None
    geometry_status: str | None = None
    conflict_type: str | None = None
    tier: str | None = None
    failure_flags: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)
