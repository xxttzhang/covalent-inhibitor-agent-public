from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EngineExecutionPlan(BaseModel):
    """Serializable boundary between the scientist and an external engine."""

    model_config = ConfigDict(extra="forbid")
    engine: str
    request_id: str
    command: list[str] = Field(min_length=1)
    input_paths: list[str]
    output_dir: str
    score_direction: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = True


class EngineAdapter(ABC):
    name = "unknown"

    @abstractmethod
    def check_availability(self) -> dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    def build_command(self, **kwargs) -> list[str]:
        raise NotImplementedError

    def prepare_input(self, *args, **kwargs):
        return None

    def parse_output(self, output_dir: str | Path):
        return []

    def validate_output(self, parsed):
        return bool(parsed)

    def build_execution_plan(
        self,
        *,
        request_id: str,
        input_paths: list[str],
        output_dir: str,
        score_direction: str,
        command_kwargs: dict[str, Any],
        dry_run: bool = True,
    ) -> EngineExecutionPlan:
        """Compile a target-independent, auditable plan without launching it."""
        return EngineExecutionPlan(
            engine=self.name,
            request_id=request_id,
            command=self.build_command(**command_kwargs),
            input_paths=input_paths,
            output_dir=output_dir,
            score_direction=score_direction,
            parameters=command_kwargs,
            dry_run=dry_run,
        )
