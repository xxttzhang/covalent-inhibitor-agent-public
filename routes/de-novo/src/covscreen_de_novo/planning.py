"""Project-bound De novo planning; never submits compute or accepts candidates."""
from pathlib import Path
from typing import Any
from .de_novo import DeNovoRequest, DeNovoGeneratorAvailability, plan_de_novo


def plan_request(
    root: Path,
    request_path: str,
    *,
    require_inputs: bool = True,
    availability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Shared project/filesystem/runtime services retain their existing authority.
    # Import at call time to keep the public compatibility module cycle-free.
    from covscreen.tool_api import (resolve_within, read_json, load_project_state,
                                   infer_de_novo_availability)
    request_abs = resolve_within(root, request_path)
    request = DeNovoRequest.model_validate(read_json(request_abs))
    project_dir, passport, assessment, route = load_project_state(root, request.project_id)
    request_abs.relative_to(project_dir)
    flags: dict[str, bool] = {}
    errors: list[str] = []
    for field in (
        "scaffold_candidates_path",
        "covalent_candidates_path",
        "optimized_candidates_path",
        "covalent_screen_results_path",
    ):
        relative = getattr(request, field)
        available = False
        if relative is not None:
            try:
                candidate = resolve_within(project_dir, relative)
                # A file is not a stage acceptance receipt. Until this generic
                # planner has a content/provenance verifier for the stage,
                # never promote unverified legacy paths to completed stages.
                # The registered PocketXMol worker has a separate real QC path.
                if candidate.is_file():
                    errors.append(
                        f"{field}: unverified stage artifact; content, request lineage "
                        "and scientific acceptance must be verified before handoff"
                    )
                elif require_inputs:
                    errors.append(f"{field} does not exist: {relative}")
            except ValueError:
                errors.append(f"{field} escapes the project directory")
        flags[field] = available
    if errors:
        return {"status": "FAIL", "validation": {"status": "FAIL", "errors": errors},
                "assessment": {"state": "not_evaluated"}, "compute_submitted": False}
    detected = (
        DeNovoGeneratorAvailability.model_validate(availability)
        if availability is not None
        else infer_de_novo_availability(root)
    )
    plan = plan_de_novo(
        request,
        passport,
        assessment,
        route,
        detected,
        scaffold_candidates_available=flags["scaffold_candidates_path"],
        covalent_candidates_available=flags["covalent_candidates_path"],
        optimized_candidates_available=flags["optimized_candidates_path"],
        covalent_screen_results_available=flags["covalent_screen_results_path"],
    )
    return {
        "status": "PASS",
        "planning_only": True,
        "compute_submitted": False,
        "availability_source": "caller_asserted" if availability is not None else "runtime_detection",
        "assessment": {"state": "not_evaluated",
            "reason": "Planning decisions and asset flags are not scientific stage acceptance or execution authorization."},
        "validation": {"status": "PASS", "errors": []},
        "plan": plan.model_dump(mode="json"),
    }

