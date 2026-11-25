"""Common route status contract; stage order and scientific gates stay in each route."""
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

Stage = TypeVar('Stage', bound=str)


class StageStatus(BaseModel, Generic[Stage]):
    model_config = ConfigDict(extra='forbid')
    stage: Stage
    engine: str
    state: str
    run_id: str | None = None
    parent_run_id: str | None = None
    quality_pass: bool | None = None
    reason: str


class ChainStatus(BaseModel, Generic[Stage]):
    model_config = ConfigDict(extra='forbid')
    schema_version: str = '1.0'
    campaign_id: str
    project_id: str
    stages: list[StageStatus[Stage]]
    next_stage: Stage | None
    next_parent_run_id: str | None
    terminal_state: Literal['ready', 'active', 'quality_gate_failed', 'dependency_quarantined', 'completed']
