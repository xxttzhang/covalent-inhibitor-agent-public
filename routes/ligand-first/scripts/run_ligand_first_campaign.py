"""Supervise an explicitly approved, bounded registered Ligand-first campaign.

Uses the existing JobManager executor; it does not provision remote resources.
Run only on a host whose execution configuration supports the requested stages.
"""
from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import fcntl
import hashlib
import json
import math
import os
import time
from pathlib import Path

from covscreen.layout import resolve_route_config
from covscreen.execution import JobManager
from covscreen.jobs import sha256_file, utc_now
from covscreen.ligand_chain import (
    compile_next_ligand_request, inspect_ligand_chain, load_ligand_chain_config,
)


def stop_reason(status, approved_run_ids: dict[str, str]) -> str | None:
    for stage in status.stages:
        if stage.run_id and approved_run_ids.get(stage.stage) != stage.run_id:
            return "unapproved_existing_run"
        if stage.state in {"FAILED", "CANCELLED", "TIMED_OUT"}:
            return "stage_failed_requires_explicit_retry"
        if stage.state == "PLANNED":
            return "planned_stage_requires_execution_approval"
    if status.terminal_state == "quality_gate_failed":
        return "quality_gate_failed"
    return None


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def verify_approved_inputs(config_path: Path, campaign_path: Path,
                           config_sha256: str, campaign_sha256: str) -> None:
    if sha256_file(config_path) != config_sha256:
        raise ValueError("Approved configuration hash does not match current configuration")
    if sha256_file(campaign_path) != campaign_sha256:
        raise ValueError("Approved campaign hash does not match current campaign")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--approved-config-sha256", required=True)
    parser.add_argument("--approved-campaign-sha256", required=True)
    parser.add_argument("--human-approved", action="store_true")
    parser.add_argument("--approval-note", required=True)
    parser.add_argument("--max-runtime-minutes", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    if not args.human_approved or not args.approval_note.strip():
        parser.error("Explicit campaign approval and its note are required")
    if not 1 <= args.max_runtime_minutes <= 10080 or not 5 <= args.poll_seconds <= 60:
        parser.error("Runtime must be 1..10080 minutes and polling 5..60 seconds")
    root, path, out = args.project_root.resolve(), args.config.resolve(), args.output_dir.resolve()
    path = resolve_route_config(root, path)
    out.relative_to(root)
    if sha256_file(path) != args.approved_config_sha256:
        parser.error("Approved configuration hash does not match current configuration")
    config = load_ligand_chain_config(path)
    campaign = (root / config.campaign_path).resolve()
    campaign.relative_to(root)
    try:
        verify_approved_inputs(path, campaign, args.approved_config_sha256,
                               args.approved_campaign_sha256)
    except ValueError as exc:
        parser.error(str(exc))
    campaign_hash = args.approved_campaign_sha256
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "campaign_controller.json"
    if state_path.exists():
        parser.error("Controller output already exists; inspect it rather than implicitly restarting")
    # Lock by project identity, since the registry groups the chain by project_id.
    lock_dir = root / "state" / "ligand_controllers"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / (hashlib.sha256(config.project_id.encode()).hexdigest() + ".lock")
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another controller already holds this project's execution lock")
        manager = JobManager(root)
        deadline = time.monotonic() + args.max_runtime_minutes * 60
        run_ids = dict(config.seed_run_ids)
        submitted = []
        state = {"schema_version": "1.0", "campaign_id": config.campaign_id,
                 "approved_config_sha256": args.approved_config_sha256,
                 "campaign_sha256": campaign_hash, "approval_note": args.approval_note,
                 "max_runtime_minutes": args.max_runtime_minutes,
                 "started_at": utc_now(), "submitted_runs": submitted}
        try:
            while True:
                reason = None
                if sha256_file(path) != args.approved_config_sha256 or sha256_file(campaign) != campaign_hash:
                    reason = "approved_inputs_changed"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = reason or "controller_deadline_reached"
                current_config = config.model_copy(update={"seed_run_ids": run_ids})
                status = inspect_ligand_chain(root, current_config)
                reason = reason or stop_reason(status, run_ids)
                if status.terminal_state == "ready" and remaining < 60:
                    reason = reason or "insufficient_remaining_stage_budget"
                state.update(updated_at=utc_now(), chain=status.model_dump(mode="json"),
                             run_ids=run_ids, status=reason or status.terminal_state)
                atomic_json(state_path, state)
                if reason:
                    # No new stage may start. Already submitted workers enforce
                    # their own remaining-time cap; they are not silently killed.
                    return 2
                if status.terminal_state == "completed":
                    return 0
                if status.terminal_state == "ready":
                    if len(submitted) >= len(config.stage_order):
                        raise RuntimeError("Stage submission ceiling exceeded")
                    request = compile_next_ligand_request(root, current_config, status,
                        execution_mode="execute", human_approved=True,
                        approval_note=args.approval_note)
                    if request is None:
                        raise RuntimeError("Ready chain has no legal request")
                    cap = min(request.resource_budget.max_runtime_minutes,
                              max(1, math.floor(remaining / 60)))
                    request.resource_budget = request.resource_budget.model_copy(
                        update={"max_runtime_minutes": cap})
                    from covscreen.native_approval import resume_campaign
                    with resume_campaign(out / 'approved_plan.json'):
                        result = manager.submit(request)
                    run_ids[status.next_stage] = result.run_id
                    submitted.append({"stage": status.next_stage, "run_id": result.run_id,
                                      "resource_budget": request.resource_budget.model_dump(mode="json")})
                    state.update(run_ids=run_ids, status="active", updated_at=utc_now())
                    atomic_json(state_path, state)
                time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
        except Exception as exc:
            state.update(status="controller_error", error=f"{type(exc).__name__}:{exc}", updated_at=utc_now())
            atomic_json(state_path, state)
            raise


if __name__ == "__main__":
    raise SystemExit(main())
