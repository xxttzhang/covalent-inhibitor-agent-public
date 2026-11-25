"""One-shot claim for a signed native VS instance task."""

from pathlib import Path
import subprocess

from .covdock_vs_runner import validate_approved_recipe
from .job_cancellation import JobCancelled
from .jobs import ArtifactRegistry, sha256_file


def inspect(root: Path, run_id: str, instance: str) -> dict:
    registry = ArtifactRegistry(root)
    job = registry.job_dir(run_id)
    spec_path = job / "spec.json"
    digest = sha256_file(spec_path)
    spec = registry.load_spec(run_id)
    packet = validate_approved_recipe(root, spec)
    from .ligand_instance import selected_instance
    if (
        {"library-first": "asgbie2", "ligand-first": selected_instance(), "de-novo": "asgbie3"}.get(spec.route) != instance
        or packet["worker_backend"] != "external-instance"
        or packet["plan"].get("execution_instance") != instance
    ):
        raise ValueError("Instance task does not authorize this worker")
    result = registry.load_result(run_id)
    if spec.run_id != run_id or result.run_id != run_id or any(
        getattr(result, field) != getattr(spec, field)
        for field in ("engine", "project_id", "route", "attempt", "parent_run_id")
    ):
        raise ValueError("Instance task state does not match its registered spec")
    if sha256_file(spec_path) != digest:
        raise ValueError("Instance task changed during validation")
    return {"run_id": run_id, "spec_sha256": digest, "execution_instance": instance,
            "state": result.state, "worker_claimed": (job / "instance_worker_claim").is_dir()}


def claim(root: Path, run_id: str, expected_digest: str, instance: str) -> Path:
    record = inspect(root, run_id, instance)
    if record["spec_sha256"] != expected_digest:
        raise ValueError("Instance task differs from its dispatch receipt")
    if record["state"] != "QUEUED":
        raise ValueError("Instance claim requires a queued task")
    registry = ArtifactRegistry(root)
    job = registry.job_dir(run_id)
    # The shared worker settles an approved cancellation before starting the engine.
    marker = job / "instance_worker_claim"
    marker.mkdir()
    return marker


def execute(root: Path, run_id: str, expected_digest: str, instance: str):
    """Execute one approved instance task from a dedicated Linux process."""
    from .covdock_vs_runner import _run_approved_recipe
    from .execution import _execute_claimed_job
    from .instance_process import InstanceProcessCancelled, InstanceProcessTree, InstanceRuntimeExceeded

    root = root.resolve(strict=True)
    tree = InstanceProcessTree()
    claim(root, run_id, expected_digest, instance)

    def run_recipe(registry, spec):
        packet = validate_approved_recipe(root, spec)
        if sha256_file(registry.job_dir(run_id) / "spec.json") != expected_digest:
            raise ValueError("Instance task changed after claim")
        try:
            _run_approved_recipe(root, registry, spec, packet, instance_tree=tree)
        except InstanceProcessCancelled as error:
            raise JobCancelled("Instance process tree stopped") from error
        except InstanceRuntimeExceeded as error:
            raise subprocess.TimeoutExpired(spec.command, packet["timeout_seconds"]) from error
        return 0

    return _execute_claimed_job(root, run_id, instance_runner=run_recipe)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--instance", required=True, choices=["asgbie2", "asgbie3", "asgbie4"])
    args = parser.parse_args()
    result = execute(args.root, args.run_id, args.digest, args.instance)
    print(result.model_dump_json())
    return 0 if result.state == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
