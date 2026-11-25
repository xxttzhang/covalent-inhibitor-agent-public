"""Fail-stop execution kernel for HOST-REGISTERED MD adapters, not an MD engine.

Run only inside an already authorized compute allocation. This module neither
grants resource approval nor creates jobs. A model must never supply a plan,
executable, or evidence callback directly. Scientific evidence comes from the
frozen, separately tested adapter; hashing is integrity, not scientific proof.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import time


STAGES = ("prepare", "parameters", "short_qc", "production", "trajectory_audit", "analysis", "report")
PREP_CHECKS = ("reactant_or_adduct_graph", "bond_orders", "nucleophile_state", "formal_charge",
               "stereochemistry", "no_severe_clash", "protein_local_structure", "pocket_retention",
               "anchor_interactions")
PARAMETER_CHECKS = ("atom_mapping", "bonded_coverage", "nonbonded_coverage", "charge_accounting",
                    "covalent_state_matches_plan", "conversion_audit")
TRAJECTORY_CHECKS = ("all_frames_read", "finite_coordinates", "time_axis", "periodic_box", "input_identity")
BASE_METRICS = {"protein_rmsd", "ligand_rmsd", "ligand_com", "pocket_contacts", "hydrogen_bonds", "physical_qc"}


def digest(file):
    value = hashlib.sha256()
    with Path(file).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read(file):
    return json.loads(Path(file).read_text(), parse_constant=lambda value: fail("Nonfinite JSON: " + value))


def fail(message):
    raise ValueError(message)


def require(condition, message):
    if not condition:
        fail(message)


def relative(root, name):
    require(isinstance(name, str) and name and not Path(name).is_absolute(), "Expected relative artifact path")
    require(all(part not in ("", ".", "..") for part in name.split("/")), "Noncanonical artifact path")
    file = root / name
    require(file.resolve().is_relative_to(root.resolve()), "Artifact escapes its root")
    for parent in [file, *file.parents]:
        if parent == root:
            break
        require(not parent.is_symlink(), "Symlinked artifacts are not accepted")
    return file


def inventory(root, files):
    require(isinstance(files, dict) and files, "Missing artifact inventory")
    for name, expected in files.items():
        require(isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected), "Invalid SHA256")
        file = relative(root, name)
        require(file.is_file() and digest(file) == expected, "Artifact hash mismatch: " + name)


def atomic(file, data):
    temporary = file.with_name(file.name + ".tmp-" + str(os.getpid()))
    with temporary.open("x") as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, file)


def plan_read(package, expected):
    require(digest(package / "plan.json") == expected, "Frozen plan changed")
    plan = read(package / "plan.json")
    require(plan["schema"] == "md-workflow-plan-v1", "Unsupported workflow plan")
    require(plan["kernel_sha256"] == digest(Path(__file__)), "Execution kernel changed")
    require(re.fullmatch(r"[A-Za-z0-9_-]{1,100}", plan["run_id"]), "Invalid run identity")
    require(plan["phase"] in ("pre_covalent", "covalent_adduct"), "Explicit covalent state required")
    require(plan["engine"] == "OpenMM" and plan["forcefield"]["family"] == "OPLS", "No engine/forcefield fallback")
    require(bool(plan["forcefield"]["version"]), "Exact OPLS version required")
    require(plan["evidence_scope"] in ("scientific", "software_test"), "Explicit evidence scope required")
    for field in ("production_ps", "frame_interval_ps", "wall_seconds"):
        value = plan[field]
        require(type(value) in (int, float) and math.isfinite(value) and value > 0, "Invalid " + field)
    frames = plan["production_ps"] / plan["frame_interval_ps"]
    require(math.isclose(frames, round(frames), abs_tol=1e-9, rel_tol=0), "Fractional planned frame count")
    require([step["name"] for step in plan["stages"]] == list(STAGES), "All workflow stages are required in order")
    require(sum(step["timeout_seconds"] for step in plan["stages"]) <= plan["wall_seconds"], "Stage limits exceed allocation")
    inventory(package, plan["files"])
    for step in plan["stages"]:
        require(type(step["timeout_seconds"]) is int and step["timeout_seconds"] > 0, "Invalid stage timeout")
        require(step["script"] in plan["files"], "Unregistered adapter script")
        require(Path(step["python"]).is_absolute() and digest(step["python"]) == step["python_sha256"], "Interpreter changed")
    return plan


def checks(data, names):
    require(all(data.get("checks", {}).get(name) is True for name in names), "Required stage checks did not pass")


def verify(folder, plan, plan_hash, stage):
    data = read(folder / "evidence.json")
    require(data["schema"] == "md-stage-evidence-v1", "Unsupported stage evidence")
    require(data["run_id"] == plan["run_id"] and data["plan_sha256"] == plan_hash, "Evidence belongs to another plan")
    require(data["stage"] == stage and data["phase"] == plan["phase"], "Evidence stage/chemical state mismatch")
    require(data["evidence_scope"] == plan["evidence_scope"], "Software test cannot count as scientific evidence")
    require(data["status"] == "PASS", "Stage is blocked or needs review")
    upstream = {name: digest(folder.parent / name / "evidence.json") for name in STAGES[:STAGES.index(stage)]}
    require(data["upstream_evidence_sha256"] == upstream, "Evidence is not bound to verified upstream stages")
    inventory(folder, data["files"])
    if stage == "prepare":
        checks(data, ("sequence_provenance", "missing_residues_resolved", "chemistry_identity"))
    if stage == "parameters":
        checks(data, PARAMETER_CHECKS)
        require(data["forcefield"] == plan["forcefield"], "Parameter forcefield differs from plan")
    if stage == "short_qc":
        checks(data, PREP_CHECKS + ("unrestrained_relaxation", "cpu_cuda_energy_force_agreement"))
        # NAC loss is an observation, deliberately NOT a preparation gate.
        if plan["phase"] == "pre_covalent":
            require(data["permanent_reactive_geometry_restraint"] is False, "Biased prereactive geometry")
    if stage in ("production", "trajectory_audit"):
        require(type(data["production_ps"]) in (int, float) and type(data["frames"]) is int,
                "Trajectory duration/frame count must be numeric, not flags")
        require(data["production_ps"] == plan["production_ps"], "Incomplete production duration")
        require(data["frames"] == round(plan["production_ps"] / plan["frame_interval_ps"]), "Incomplete trajectory")
        require(data["unrestrained_production"] is True, "Production restraints differ from protocol")
    if stage == "trajectory_audit":
        checks(data, TRAJECTORY_CHECKS)
    if stage == "analysis":
        require(BASE_METRICS <= set(data["metrics"]), "Required analysis is missing")
        if plan["phase"] == "pre_covalent":
            require({"nac", "gating"} <= set(data["metrics"]), "Pre-covalent gating analysis missing")
            checks(data, ("reaction_atom_mapping", "gating_definition_frozen"))
        else:
            require("covalent_geometry" in data["metrics"] and not {"nac", "gating"} & set(data["metrics"]),
                    "An adduct trajectory is not prereactive gating evidence")
        require(data["affinity_or_kinact_proven"] is False, "MD stability cannot establish affinity or kinact")
    if stage == "report":
        require({"report", "tables", "figures"} <= set(data["roles"]), "Report, tables and figures are required")
        require(all(name in data["files"] for name in data["roles"].values()), "Unwitnessed report artifact")
        require(data["scientific_review"] == "pending", "Automatic reporting is not expert review")
    return digest(folder / "evidence.json")


def terminate(child):
    if child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def run(package, output, expected):
    package = Path(package).resolve()
    output = Path(output).resolve()
    plan = plan_read(package, expected)
    require(not output.is_relative_to(package) and not package.is_relative_to(output), "Inputs and outputs must be disjoint")
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        status_path = output / "status.json"
        state = read(status_path) if status_path.exists() else {
            "schema": "md-workflow-state-v1", "run_id": plan["run_id"], "plan_sha256": expected,
            "state": "READY", "stages": {}, "elapsed_seconds": 0,
            "evidence_scope": plan["evidence_scope"], "resource_submission_performed": False,
        }
        require(state["plan_sha256"] == expected, "Output belongs to another plan")
        require(state["state"] not in ("RUNNING", "NEEDS_REVIEW"), "Interrupted or failed stage: inspect; never replay automatically")
        start = time.monotonic()
        prior_elapsed = state["elapsed_seconds"]
        child = None
        previous_handler = signal.getsignal(signal.SIGTERM)
        def interrupted(*_):
            raise InterruptedError("Workflow terminated; preserve partial outputs for review")
        signal.signal(signal.SIGTERM, interrupted)
        try:
            for step in plan["stages"]:
                name = step["name"]
                plan_read(package, expected)
                # Revalidate every upstream witness before a downstream command.
                for previous, receipt in state["stages"].items():
                    if receipt["state"] == "VERIFIED":
                        require(verify(output / previous, plan, expected, previous) == receipt["evidence_sha256"],
                                "Previously accepted evidence changed")
                if state["stages"].get(name, {}).get("state") == "VERIFIED":
                    continue
                remaining = plan["wall_seconds"] - prior_elapsed - (time.monotonic() - start)
                require(remaining > 0, "Frozen wall-time budget exhausted")
                folder = output / name
                folder.mkdir()  # Never silently reuse a partial output directory.
                state["state"] = "RUNNING"
                state["current_stage"] = name
                state["stages"][name] = {"state": "RUNNING", "started_at": time.time()}
                atomic(status_path, state)
                env = {key: value for key, value in os.environ.items()
                       if key not in {"LD_PRELOAD", "LD_AUDIT", "PYTHONHOME", "PYTHONPATH"}}
                env.update(PYTHONDONTWRITEBYTECODE="1", MD_WORKFLOW_PLAN_SHA256=expected)
                # argv only; registered adapters receive fixed paths, never model shell text.
                command = [step["python"], str(relative(package, step["script"])),
                           "--plan", str(package / "plan.json"), "--stage", name,
                           "--output", str(folder), "--upstream", str(output)]
                with (folder / "stdout.log").open("xb") as stream:
                    child = subprocess.Popen(command, cwd=package, env=env, stdin=subprocess.DEVNULL,
                                             stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                    code = child.wait(timeout=min(step["timeout_seconds"], remaining))
                require(code == 0, "Adapter failed: " + name)
                plan_read(package, expected)
                receipt = verify(folder, plan, expected, name)
                state["stages"][name].update(state="VERIFIED", evidence_sha256=receipt, finished_at=time.time())
                state["elapsed_seconds"] = prior_elapsed + time.monotonic() - start
                state["state"] = "READY"
                atomic(status_path, state)
            for name, receipt in state["stages"].items():
                require(verify(output / name, plan, expected, name) == receipt["evidence_sha256"],
                        "Evidence changed during downstream execution")
            state["state"] = ("SOFTWARE_TEST_COMPLETE_NOT_MD" if plan["evidence_scope"] == "software_test"
                              else "COMPLETE_REQUIRES_SCIENTIFIC_REVIEW")
        except BaseException as error:
            if child is not None:
                terminate(child)
            state["state"] = "NEEDS_REVIEW"
            state["error"] = str(error)
            raise
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
            state["elapsed_seconds"] = prior_elapsed + time.monotonic() - start
            state["updated_at"] = time.time()
            atomic(status_path, state)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--plan-sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.package, args.output, args.plan_sha256), allow_nan=False))
