from __future__ import annotations

from .layout import library_script, resolve_library_config, resolve_route_config, config_reference, ligand_script, science_script

import csv
import importlib.util
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml
from rdkit import Chem

from .attachment import AttachmentRequest, plan_attachments
from .covalent_enumerator import CovalentEnumerationRequest, enumerate_covalent_candidates
from .cascade import (
    AdaptiveCascadePlan,
    CascadeObservation,
    CascadePolicy,
    EngineAvailability,
    choose_next_action,
    plan_library_first_cascade,
)
from .ligand_first import LigandFirstRequest, plan_ligand_first
from .ligand_first_geometry import LigandFirstGeometryResult, load_sdf_poses
from .ligand_first_inputs import build_parent_pose_mol, load_campaign
from .ligand_first_passport import independently_evaluate_candidate
from .de_novo import DeNovoGeneratorAvailability, DeNovoRequest, plan_de_novo
from .execution import JobManager
from .jobs import ArtifactRegistry, JobSubmissionRequest, ResourceBudget, sha256_file
from .runtime_health import resolve_openscience_config, resolve_openscience_port, summarize_runtime_preflight
from .library_intake import resolve_library_first_intake
from .library_chain import compile_next_request, inspect_chain, load_chain_config
from .ligand_intake import resolve_ligand_first_intake
from .structure_discovery import ENDPOINT as RCSB_SEARCH_ENDPOINT, build_query, discover
from .structure_site_audit import audit_structure_site
from .ligand_identity import identity_plan, resolve_names
from .target_identity import target_plan, resolve_target, load_target_identity
from .structure_discovery import build_target_query, discover_target, load_target_discovery
from .structure_acquisition import acquisition_plan, acquire_structure, target_acquisition_plan, acquire_target_structure
from .native_case import prepare_native_case
from .ligand_chain import (
    STAGE_ENGINES,
    compile_next_ligand_request,
    inspect_ligand_chain,
    load_ligand_chain_config,
)
from .research import (
    CovalentAssessment,
    LibraryFirstRequest,
    RoutePlan,
    TargetPassport,
    read_json,
    validate_library_first_request,
    validate_project,
)
from .schrodinger import validate_license_config


def resolve_within(root: Path, relative: str | Path) -> Path:
    root = root.resolve()
    path = Path(relative)
    candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
    candidate.relative_to(root)
    return candidate


def load_project_state(root: Path, project_id: str) -> tuple[Path, TargetPassport, CovalentAssessment, RoutePlan]:
    project_dir = resolve_within(root, Path("projects") / project_id)
    if project_dir.name != project_id:
        raise ValueError("project_id/path mismatch")
    passport = TargetPassport.model_validate(read_json(project_dir / "target_passport.json"))
    assessment = CovalentAssessment.model_validate(
        read_json(project_dir / "assessment" / "covalent_assessment.json")
    )
    route = RoutePlan.model_validate(read_json(project_dir / "plans" / "route_plan.json"))
    return project_dir, passport, assessment, route


def count_library_rows(path: Path) -> int:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = sum(1 for _ in csv.reader(handle, delimiter=delimiter)) - 1
        return max(0, rows)
    if suffix == ".parquet":
        import pyarrow.parquet as pq

        return int(pq.ParquetFile(path).metadata.num_rows)
    raise ValueError("library_size is required unless ligand table is CSV, TSV, or Parquet")


def infer_engine_availability(root: Path) -> EngineAvailability:
    from .docking_policy import POLICY
    config = yaml.safe_load((root / "configs" / "local.inspire.yaml").read_text()) or {}
    carsidock = config.get("carsidock", {})
    af3 = config.get("alphafold3", {})
    schrodinger = config.get("schrodinger", {})
    carsidock_ready = all(
        bool(carsidock.get(key)) and (
            Path(carsidock[key]).is_file() if key in {"checkpoint", "python"} else Path(carsidock[key]).is_dir())
        for key in ("repo", "python", "checkpoint")
    )
    af3_ready = (
        Path(af3.get("runner", "")).is_file()
        and Path(af3.get("python", "")).is_file()
        and bool(af3.get("database_dir")) and Path(af3["database_dir"]).is_dir()
        and bool(af3.get("model_dir")) and Path(af3["model_dir"]).is_dir()
    )
    license_ok, _ = validate_license_config(schrodinger.get("license_file", ""))
    covdock_ready = bool(schrodinger.get("root")) and Path(schrodinger["root"], "covalent_docking").is_file()
    return EngineAvailability(
        carsidock_cov=carsidock_ready and "carsidock-cov" not in POLICY["disabled_engines"],
        rtmscore=carsidock_ready and Path(carsidock.get("rtmscore_checkpoint", "")).is_file(),
        alphafold3_mpae=af3_ready,
        covdock_vs=covdock_ready,
        covdock_lo=covdock_ready,
        schrodinger_license_ok=license_ok,
    )


def infer_de_novo_availability(root: Path) -> DeNovoGeneratorAvailability:
    config = yaml.safe_load((root / "configs" / "local.inspire.yaml").read_text()) or {}
    generators = config.get("de_novo", {})

    def ready(name: str) -> bool:
        item = generators.get(name, {})
        if not item.get("enabled", False):
            return False
        required = [item.get("repo"), item.get("checkpoint")]
        required = [Path(value) for value in required if value]
        return bool(required) and all(path.exists() for path in required)

    return DeNovoGeneratorAvailability(
        pocketxmol=ready("pocketxmol"),
        pocket2mol=ready("pocket2mol"),
        targetdiff=ready("targetdiff"),
        graphbp=ready("graphbp"),
        covagen=ready("covagen") and bool(generators.get("covagen", {}).get("adapter_verified")),
        covalentizer_rdkit=ready("covalentizer_rdkit") and bool(
            generators.get("covalentizer_rdkit", {}).get("adapter_verified")
        ),
        rdkit=importlib.util.find_spec("rdkit") is not None,
        reinvent4=ready("reinvent4"),
        difflinker=ready("difflinker"),
        carsidock_cov=infer_engine_availability(root).carsidock_cov,
    )


# Preserve the private import used by integrations while keeping the probe standalone.
from .runtime_probe import probe_python_runtime as _probe_python_runtime, timeout_seconds


def runtime_preflight(root: Path) -> dict[str, Any]:
    config = yaml.safe_load((root / "configs" / "local.inspire.yaml").read_text()) or {}
    configured = config.get("carsidock", {}).get("python")
    executable = Path(configured) if configured else Path(os.sys.executable)
    probe = _probe_python_runtime(executable, timeout=timeout_seconds(config))
    availability = infer_engine_availability(root)
    return summarize_runtime_preflight(
        probe=probe,
        engines=availability.model_dump(mode="json"),
        executable=executable,
    )


def platform_health_tool(root: Path) -> dict[str, Any]:
    """Fast health snapshot without launching a scientific engine."""
    root = root.resolve()
    config_path = root / "configs" / "local.inspire.yaml"
    openscience_config, config_candidates = resolve_openscience_config(root)
    from .source_release import frozen_release
    source_release = frozen_release()
    required_scripts = [science_script(name) for name in (
        'run_job_worker.py', 'run_rdkit_enumeration.py', 'prepare_ligand_first_inputs.py',
        'generate_real_ligand_first_candidates.py', 'run_ligand_first_covdock_grouped.py',
        'evaluate_ligand_first_batch.py', 'compile_ligand_first_chain.py',
        'run_pocketxmol_registered.py', 'run_reinvent4_registered.py', 'run_difflinker_registered.py')]
    required_scripts.append(library_script(root, 'run_covindb_prescreen.py'))
    checks: dict[str, Any] = {
        "project_root": root.is_dir(),
        "project_config": config_path.is_file(),
        "openscience_config": openscience_config is not None,
        "service_scripts": all(path.is_file() for path in required_scripts),
        "rdkit_importable": importlib.util.find_spec("rdkit") is not None,
    }
    port = resolve_openscience_port()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            checks["openscience_http_port"] = True
    except OSError:
        checks["openscience_http_port"] = False
    registry = ArtifactRegistry(root, create=False)
    registry_parent = registry.root.parent
    checks["job_registry"] = (
        registry.root.is_dir() and os.access(registry.root, os.W_OK)
    ) or (
        not registry.root.exists()
        and registry_parent.is_dir()
        and os.access(registry_parent, os.W_OK)
    )
    disk = shutil.disk_usage(root)
    checks["disk_free_gb"] = round(disk.free / 1024**3, 2)
    config = yaml.safe_load(config_path.read_text()) if config_path.is_file() else {}
    carsidock = config.get("carsidock", {})
    checks["carsidock_assets"] = bool(
        Path(carsidock.get("repo", "")).is_dir()
        and Path(carsidock.get("checkpoint", "")).is_file()
    )
    checks["rtmscore_assets"] = bool(Path(carsidock.get("rtmscore_checkpoint", "")).is_file())
    covindb = config.get("covindb", {})
    checks["covindb_library"] = bool(
        Path(covindb.get("virtual_screening_library", "")).is_file()
    )
    generators = config.get("de_novo", {})
    for name in ("pocketxmol", "reinvent4", "difflinker"):
        item = generators.get(name, {})
        checks[f"{name}_assets"] = bool(
            item.get("enabled", False)
            and Path(item.get("repo", "")).is_dir()
            and Path(item.get("python", "")).is_file()
            and Path(item.get("checkpoint", "")).is_file()
        )
    hard_checks = [
        "project_root", "project_config", "openscience_config", "service_scripts",
        "rdkit_importable", "job_registry", "carsidock_assets", "rtmscore_assets",
        "covindb_library",
        "pocketxmol_assets", "reinvent4_assets", "difflinker_assets",
    ]
    failures = [name for name in hard_checks if not checks.get(name)]
    if failures:
        status = "FAIL"
    elif not checks["openscience_http_port"]:
        status = "DEGRADED"
    else:
        status = "PASS"
    return {
        "status": status,
        "checks": checks,
        "failures": failures,
        "execution_boundary": "Health checks do not launch scientific calculations.",
        "openscience_config_path": str(openscience_config) if openscience_config else None,
        "openscience_config_candidates_checked": config_candidates,
        "openscience_http_port_checked": port,
        "data_root": str(root),
        "source_release": source_release,
    }


def submit_job_tool(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    request = JobSubmissionRequest.model_validate(payload)
    manager = JobManager(root)
    result = manager.submit(request)
    spec = manager.registry.load_spec(result.run_id)
    return {
        "status": "PASS",
        "job": result.model_dump(mode="json"),
        "spec": spec.model_dump(mode="json"),
    }


def library_first_chain_tool(
    root: Path,
    config_path: str,
    *,
    execution_mode: str = "plan",
    submit: bool = False,
    human_approved: bool = False,
    approval_note: str | None = None,
) -> dict[str, Any]:
    """Inspect the formal parent-linked chain and optionally submit its sole legal next stage."""

    if execution_mode not in {"plan", "execute"}:
        raise ValueError("execution_mode must be plan or execute")
    config_abs = resolve_library_config(root, config_path)
    config = load_chain_config(config_abs)
    status = inspect_chain(root, config)
    from .library_chain import STAGE_ORDER, STAGE_ENGINES
    route_stages = STAGE_ORDER[:STAGE_ORDER.index(config.terminal_stage) + 1]
    request = compile_next_request(
        root,
        config,
        status,
        execution_mode=execution_mode,
        human_approved=human_approved,
        approval_note=approval_note,
    )
    response: dict[str, Any] = {
        "status": "PASS",
        "chain": status.model_dump(mode="json"),
        "route_plan": {"route": "library-first", "terminal_stage": config.terminal_stage,
                       "stages": [{"stage": stage, "engine": STAGE_ENGINES[stage],
                                   "execution_requires_native_approval": True} for stage in route_stages],
                       "policy": "Compile the next stage only from verified upstream results; do not execute beyond terminal_stage."},
        "next_request": request.model_dump(mode="json") if request else None,
        "submitted": False,
    }
    if status.terminal_state == 'dependency_quarantined':
        response['status'] = 'BLOCKED'
        response['next_action'] = 'Report the quarantined dependency; do not retry or bypass it. Independent workflows may continue.'
    from .plan_readiness import plan_readiness
    response['execution_readiness'] = plan_readiness(root, request)
    if submit:
        if request is None:
            raise ValueError("the chain has no ready next stage to submit")
        result = JobManager(root).submit(request)
        spec = JobManager(root).registry.load_spec(result.run_id)
        response.update(
            {
                "submitted": True,
                "job": result.model_dump(mode="json"),
                "spec": spec.model_dump(mode="json"),
            }
        )
    from .workflow_state import persist_chain_observation
    response["workflow_state"] = persist_chain_observation(root, config_abs, response)
    return response


def library_first_intake_tool(
    root: Path,
    query: str,
    *,
    source_library: str = "CovalentInDB 2.0",
    scope: str = "auto",
) -> dict[str, Any]:
    """Resolve a natural-language screening request against frozen target and campaign registries."""

    if scope not in {"auto", "pilot", "full"}:
        raise ValueError("scope must be auto, pilot, or full")
    intake = resolve_library_first_intake(
        root,
        query,
        source_library=source_library,
        scope=scope,  # type: ignore[arg-type]
    )
    return {"status": "PASS", "intake": intake.model_dump(mode="json")}


def ligand_first_intake_tool(root: Path, query: str) -> dict[str, Any]:
    """Resolve natural language to one registered target/site/parent-ligand campaign."""

    intake = resolve_ligand_first_intake(root, query)
    result = {"status": "PASS", "intake": intake.model_dump(mode="json")}
    if intake.state in {"TARGET_NOT_REGISTERED", "LIGAND_NOT_REGISTERED"}:
        result["identity_resolution"] = {
            "tool": "covalent_ligand_first_resolve_names",
            "requires": ["gene symbol", "explicit taxon ID", "canonical Cys position", "ligand name"],
        }
        result["structure_discovery"] = {
            "tool": "covalent_ligand_first_discover_structures",
            "requires": ["verified UniProt accession", "verified CCD component ID"],
            "note": "Resolve names and species before search. Discovery does not register or approve a campaign.",
        }
    return result


def library_first_resolve_target_tool(root: Path, gene: str, taxon: int, *, action: str = "plan") -> dict[str, Any]:
    if action not in {"plan", "search"}:
        raise ValueError("action must be plan or search")
    plan = target_plan(gene, taxon)
    if action == "plan":
        return {"status": "PASS", "plan": plan, "network_request_sent": False,
                "campaign_registered": False, "calculation_submitted": False}
    identity = uuid.uuid4().hex
    directory = resolve_within(root, Path("state/target_identity") / identity)
    result = resolve_target(gene, taxon, directory)
    return {"status": "FAIL" if result["status"] == "lookup_failed" else "PASS",
            "artifact_id": "target-" + identity,
            "artifact_directory": str(directory.relative_to(root.resolve())),
            "manifest_sha256": sha256_file(directory / "target_identity.json"),
            "resolution": result, "campaign_registered": False, "calculation_submitted": False}


def _load_target_artifact(root: Path, target_artifact_id: str, target_manifest_sha256: str):
    if not isinstance(target_artifact_id, str) or not re.fullmatch(r"target-[a-f0-9]{32}", target_artifact_id):
        raise ValueError("Use a target artifact ID returned by the identity tool")
    relative = Path("state/target_identity") / target_artifact_id[7:]
    if any((root / parent).is_symlink() for parent in (Path("state"), Path("state/target_identity"), relative)):
        raise ValueError("Target artifact directory cannot be a symbolic link")
    target_dir = resolve_within(root, relative)
    return load_target_identity(target_dir, target_manifest_sha256)


def library_first_discover_structures_tool(root: Path, target_artifact_id: str,
    target_manifest_sha256: str, *, limit: int = 20, action: str = "plan") -> dict[str, Any]:
    if action not in {"plan", "search"}:
        raise ValueError("action must be plan or search")
    target = _load_target_artifact(root, target_artifact_id, target_manifest_sha256)
    uniprot = target["identity"]["unique_verified_target"]
    query = build_target_query(uniprot, limit)
    plan = {"target_artifact_id": target_artifact_id, "target_manifest_sha256": target_manifest_sha256,
            "gene": target["plan"]["gene"], "taxon": target["plan"]["taxon"],
            "uniprot": uniprot, "query": query, "max_calls": 1,
            "purpose": "Candidate structures only; chain, construct, site and receptor preparation remain unverified."}
    if action == "plan":
        return {"status": "PASS", "plan": plan, "network_request_sent": False,
                "campaign_registered": False, "calculation_submitted": False}
    identity = uuid.uuid4().hex
    directory = resolve_within(root, Path("state/target_structures") / identity)
    result = {"schema": "target-structure-discovery-v1", "plan": plan,
              "campaign_ready": False, "calculation_submitted": False}
    try:
        result["discovery"] = discover_target(uniprot, limit, directory)
        result["status"] = "structures_discovered_validation_required"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result.update(status="discovery_failed", error=str(exc))
    manifest = directory / "target_structure_discovery.json"
    manifest.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return {"status": "FAIL" if result["status"] == "discovery_failed" else "PASS",
            "artifact_id": "structures-" + identity, "manifest_sha256": sha256_file(manifest),
            "artifact_directory": str(directory.relative_to(root.resolve())),
            "result": result, "campaign_registered": False, "calculation_submitted": False}


def library_first_acquire_structure_tool(root: Path, discovery_artifact_id: str,
    discovery_manifest_sha256: str, entry: str, cys: int, *, label_chain: str | None = None,
    model: int | None = None, altloc: str | None = None, action: str = "plan") -> dict[str, Any]:
    if action not in {"plan", "acquire"}:
        raise ValueError("action must be plan or acquire")
    if not isinstance(discovery_artifact_id, str) or not re.fullmatch(r"structures-[a-f0-9]{32}", discovery_artifact_id):
        raise ValueError("Use a structure-discovery artifact ID returned by the tool")
    relative = Path("state/target_structures") / discovery_artifact_id[11:]
    if any((root / p).is_symlink() for p in (Path("state"), Path("state/target_structures"), relative)):
        raise ValueError("Structure artifact directory cannot be a symbolic link")
    parent = load_target_discovery(resolve_within(root, relative), discovery_manifest_sha256)
    identity = _load_target_artifact(root, parent["plan"]["target_artifact_id"],
                                    parent["plan"]["target_manifest_sha256"])
    binding = {"gene": identity["plan"]["gene"], "taxon": identity["plan"]["taxon"],
               "uniprot": identity["identity"]["unique_verified_target"],
               "target_artifact_id": parent["plan"]["target_artifact_id"],
               "target_manifest_sha256": parent["plan"]["target_manifest_sha256"]}
    if any(parent["plan"][key] != binding[key] for key in ("gene", "taxon", "uniprot")):
        raise ValueError("Structure discovery differs from its parent target identity")
    if entry not in parent["discovery"]["entry_ids"]:
        raise ValueError("Requested entry is not in the verified discovery artifact")
    candidate, = identity["identity"]["candidates"]
    plan = target_acquisition_plan(entry, binding["uniprot"], cys)
    if cys not in candidate["canonical_cysteines"]:
        raise ValueError("Selected canonical position is not Cys in the verified sequence")
    if label_chain is not None and (not isinstance(label_chain, str) or not re.fullmatch('[A-Za-z0-9]{1,8}', label_chain)):
        raise ValueError("Invalid label chain")
    if model is not None and (type(model) is not int or model < 1):
        raise ValueError("Invalid coordinate model")
    if altloc is not None and (not isinstance(altloc, str) or not re.fullmatch('[A-Za-z0-9]', altloc)):
        raise ValueError("Invalid alternate location")
    selection = {"label_chain": label_chain, "model": model, "altloc": altloc}
    provenance = {"discovery_artifact_id": discovery_artifact_id,
                  "discovery_manifest_sha256": discovery_manifest_sha256,
                  "target_artifact_id": binding["target_artifact_id"],
                  "target_manifest_sha256": binding["target_manifest_sha256"]}
    if action == "plan":
        return {"status": "PASS", "plan": plan, "selection": selection, "parents": provenance,
                "network_request_sent": False, "calculation_submitted": False, "campaign_registered": False}
    identifier = uuid.uuid4().hex
    directory = resolve_within(root, Path("state/target_acquisition") / identifier)
    result = acquire_target_structure(entry, binding["uniprot"], cys, directory, **selection)
    construct = None
    if result["status"] == "acquired_coordinate_audit_completed_review_pending":
        from .construct_audit import audit_construct
        try:
            sequence_path = resolve_within(root, Path("state/target_identity") /
                binding["target_artifact_id"][7:] / candidate["sequence_path"])
            fasta = sequence_path.read_bytes()
            if hashlib.sha256(fasta).hexdigest() != candidate["fasta_sha256"]:
                raise ValueError("Canonical FASTA changed after target verification")
            sequence = ''.join(fasta.decode().splitlines()[1:])
            mapping, = result["mappings"]
            entity_name = f'entity_{mapping["entity_id"]}.json'
            source, = [s for s in result["sources"] if s["path"] == entity_name]
            entity_bytes = (directory / entity_name).read_bytes()
            if hashlib.sha256(entity_bytes).hexdigest() != source["sha256"]:
                raise ValueError("Construct source changed after acquisition")
            construct = audit_construct(json.loads(entity_bytes), sequence,
                                        binding["uniprot"], binding["taxon"])
        except (ValueError, KeyError, TypeError, OSError) as exc:
            construct = {"status": "construct_audit_failed", "error": str(exc),
                         "receptor_prepared": False, "campaign_ready": False}
    manifest = directory / "target_acquisition.json"
    record = {"schema": "target-acquisition-v1", "parents": provenance,
              "selection": selection, "acquisition": result, "construct_audit": construct, "receptor_prepared": False,
              "campaign_ready": False, "calculation_submitted": False}
    manifest.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    passed = result["status"] == "acquired_coordinate_audit_completed_review_pending"
    passed = passed and construct is not None and construct["status"] != "construct_audit_failed"
    return {"status": "PASS" if passed else "FAIL", "artifact_id": "receptor-input-" + identifier,
            "artifact_directory": str(directory.relative_to(root.resolve())),
            "manifest_sha256": sha256_file(manifest), "result": record,
            "campaign_registered": False, "calculation_submitted": False}


def _receptor_preparation_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    required = {"receptor_input_artifact_id", "manifest_sha256"}
    missing = sorted(required - set(arguments))
    if missing:
        raise ValueError(f"Receptor preparation missing arguments: {missing}")
    payload = {
        "receptor_input_artifact_id": arguments["receptor_input_artifact_id"],
        "manifest_sha256": arguments["manifest_sha256"],
        "altlocs": arguments.get("altlocs", {}),
        "heavy_seed": arguments.get("heavy_seed", 20261013),
        "max_missing_residues": arguments.get("max_missing_residues", 30),
    }
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    budget = ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes": 15, "gpu_count": 0, "cpu_count": 1,
        "memory_gb": 4, "max_candidates": 1,
    })
    return JobSubmissionRequest(
        request_id=f"receptor-preparation-{identity}",
        project_id=str(arguments["receptor_input_artifact_id"]),
        route="library-first", engine="library-first-receptor-preparation",
        parameters=payload, resource_budget=budget, execution_mode=execution_mode,
        human_approved=bool(arguments.get("human_approved", False)),
        approval_note=arguments.get("approval_note"),
    )


def _receptor_repair_capability() -> dict[str, Any]:
    """Describe the registered backend without probing or creating project state."""
    return {
        "selected_backend": "pdbfixer-openmm",
        "pdbfixer": {"source": "bundled_release_hashed", "version": "1.12.0"},
        "openmm": {"source": "registered_runtime", "version_verified_during_plan": True},
        "prime": {
            "implemented": False,
            "status": "capability_not_implemented",
            "use_case": "missing side chains or loops near the pocket or target cysteine",
        },
        "multiple_repair_backends_in_one_lineage_allowed": False,
    }


def library_first_prepare_receptor_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Compile, start, or inspect the registered receptor preparation job."""
    action = arguments.get("action", "plan")
    if action == "status":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("status requires run_id")
        manager = JobManager(root)
        spec = manager.registry.load_spec(run_id)
        if spec.engine != "library-first-receptor-preparation":
            raise ValueError("run_id is not a receptor preparation job")
        result = manager.registry.load_result(run_id)
        response = {"status": "PASS", "job": result.model_dump(mode="json"),
                    "spec": spec.model_dump(mode="json"),
                    "receptor_repair": _receptor_repair_capability()}
        pipeline = manager.registry.job_dir(run_id) / "artifacts/receptor-preparation/pipeline.json"
        if pipeline.is_file():
            response["pipeline"] = json.loads(pipeline.read_text())
            response["pipeline_sha256"] = sha256_file(pipeline)
        return response
    if action not in {"plan", "start"}:
        raise ValueError("action must be plan, start, or status")
    alternate_location_plan = None
    if action == "plan":
        from .receptor_conformer import plan_alternate_locations
        from .receptor_input import load_receptor_preparation_input
        prepared_input = load_receptor_preparation_input(
            root, arguments.get("receptor_input_artifact_id"),
            arguments.get("manifest_sha256"),
        )
        acquired = prepared_input["acquisition"]
        mapping, = acquired["acquisition"]["mappings"]
        alternate_location_plan = plan_alternate_locations(
            prepared_input["acquisition_directory"] / "structure.pdb", mapping,
            acquired["selection"].get("model"),
        )
        supplied = arguments.get("altlocs", {})
        required = set(alternate_location_plan["required_author_residue_ids"])
        if set(supplied) != required:
            return {
                "status": "ALTERNATE_LOCATION_SELECTION_REQUIRED",
                "alternate_location_plan": alternate_location_plan,
                "supplied_author_residue_ids": sorted(supplied),
                "receptor_repair": _receptor_repair_capability(),
                "calculation_submitted": False, "receptor_prepared": False,
                "campaign_ready": False,
            }
    request = _receptor_preparation_request(
        root, arguments, execution_mode="plan" if action == "plan" else "execute"
    )
    manager = JobManager(root)
    if action == "plan":
        spec = manager.build_spec(request)
        return {"status": "PLANNED", "spec": spec.model_dump(mode="json"),
                "alternate_location_plan": alternate_location_plan,
                "receptor_repair": _receptor_repair_capability(),
                "calculation_submitted": False, "receptor_prepared": False,
                "campaign_ready": False}
    result = manager.submit(request)
    spec = manager.registry.load_spec(result.run_id)
    return {"status": "QUEUED", "job": result.model_dump(mode="json"),
            "spec": spec.model_dump(mode="json"),
            "receptor_repair": _receptor_repair_capability(), "receptor_prepared": False,
            "campaign_ready": False}


def library_first_assess_receptor_context_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Measure a prepared Cys site against its preserved deposited environment."""
    from .receptor_input import load_receptor_preparation_input
    from .receptor_site_context import SITE_CONTEXT_POLICY_V1, assess_receptor_site_context

    required = {"receptor_input_artifact_id", "manifest_sha256", "preparation_run_id",
                "prepared_receptor_manifest_sha256"}
    missing = sorted(required - set(arguments))
    if missing:
        raise ValueError(f"Receptor context assessment missing arguments: {missing}")
    root = Path(root).resolve()
    loaded = load_receptor_preparation_input(root, arguments["receptor_input_artifact_id"],
                                             arguments["manifest_sha256"])
    manager = JobManager(root)
    run_id = arguments["preparation_run_id"]
    spec = manager.registry.load_spec(run_id)
    result = manager.registry.load_result(run_id)
    if (spec.engine != "library-first-receptor-preparation"
            or result.state != "COMPLETED" or result.engine != spec.engine):
        raise ValueError("A completed receptor-preparation job is required")
    prepared = manager.registry.job_dir(run_id) / "artifacts/receptor-preparation/prepared"
    prepared_manifest = prepared / "prepared-receptor.json"
    if (not prepared_manifest.is_file() or sha256_file(prepared_manifest)
            != arguments["prepared_receptor_manifest_sha256"]):
        raise ValueError("Prepared receptor manifest checksum mismatch")
    identity = hashlib.sha256(json.dumps({
        "receptor_input_artifact_id": arguments["receptor_input_artifact_id"],
        "target_acquisition_sha256": arguments["manifest_sha256"],
        "preparation_run_id": run_id,
        "prepared_receptor_sha256": arguments["prepared_receptor_manifest_sha256"],
        "policy": SITE_CONTEXT_POLICY_V1,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
    output = root / "state/receptor_site_context" / identity
    context_path = output / "site-context.json"
    if context_path.is_file() and not context_path.is_symlink():
        context = json.loads(context_path.read_text())
        parents = context.get("parents", {})
        if (context.get("schema") != "receptor-site-context-artifact-v1"
                or parents.get("target_acquisition_sha256") != arguments["manifest_sha256"]
                or parents.get("prepared_receptor_sha256")
                != arguments["prepared_receptor_manifest_sha256"]):
            raise ValueError("Existing receptor context artifact differs from requested parents")
    else:
        context = assess_receptor_site_context(loaded["acquisition_directory"],
            arguments["manifest_sha256"], loaded["acquisition"], prepared,
            arguments["prepared_receptor_manifest_sha256"], output)
    return {"status": "PASS", "artifact_id": "receptor-context-" + identity,
        "artifact_directory": str(output.relative_to(root)),
        "manifest_sha256": sha256_file(context_path), "result": context,
        "campaign_ready": False, "calculation_submitted": False}


def _adduct_parameterization_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    legacy=all(key in arguments for key in ("adduct_input_artifact_id","manifest_sha256"))
    registered=all(key in arguments for key in ("adduct_admission_run_id","adduct_input_manifest_sha256"))
    if legacy==registered:
        raise ValueError("Provide exactly one admitted source: registered admission run or legacy artifact")
    payload={"native_timeout_seconds":arguments.get("native_timeout_seconds",1200)}
    if registered:
        payload.update(adduct_admission_run_id=arguments["adduct_admission_run_id"],
            adduct_input_manifest_sha256=arguments["adduct_input_manifest_sha256"])
        project_id=str(arguments["adduct_admission_run_id"]); parent=project_id
    else:
        payload.update(adduct_input_artifact_id=arguments["adduct_input_artifact_id"],
            manifest_sha256=arguments["manifest_sha256"])
        project_id=str(arguments["adduct_input_artifact_id"]); parent=None
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    budget = ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes": 30, "gpu_count": 0, "cpu_count": 2,
        "memory_gb": 8, "max_candidates": 1})
    return JobSubmissionRequest(request_id=f"adduct-parameterization-{identity}",
        project_id=project_id, route="library-first",
        engine="library-first-adduct-parameterization", parameters=payload,
        resource_budget=budget, execution_mode=execution_mode,
        human_approved=bool(arguments.get("human_approved", False)),
        approval_note=arguments.get("approval_note"),parent_run_id=parent)


def library_first_parameterize_adduct_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Compile, start or inspect one native candidate-adduct parameterization."""
    action = arguments.get("action", "plan")
    manager = JobManager(root)
    if action == "status":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("status requires run_id")
        spec = manager.registry.load_spec(run_id)
        if spec.engine != "library-first-adduct-parameterization":
            raise ValueError("run_id is not an adduct-parameterization job")
        result = manager.registry.load_result(run_id)
        response = {"status": "PASS", "job": result.model_dump(mode="json"),
                    "spec": spec.model_dump(mode="json"), "md_system_ready": False}
        folder = manager.registry.job_dir(run_id) / "artifacts/adduct-parameterization"
        for name in ("parameterization-audit.json", "finished.json"):
            path = folder / name
            if path.is_file():
                response[name.removesuffix(".json").replace("-", "_")] = json.loads(path.read_text())
                response[name.removesuffix(".json").replace("-", "_") + "_sha256"] = sha256_file(path)
        return response
    if action not in {"plan", "start"}:
        raise ValueError("action must be plan, start, or status")
    request = _adduct_parameterization_request(root, arguments,
        execution_mode="plan" if action == "plan" else "execute")
    if action == "plan":
        spec = manager.build_spec(request)
        return {"status": "PLANNED", "spec": spec.model_dump(mode="json"),
                "calculation_submitted": False, "parameterization_completed": False,
                "md_system_ready": False}
    result = manager.submit(request)
    spec = manager.registry.load_spec(result.run_id)
    return {"status": "QUEUED", "job": result.model_dump(mode="json"),
        "spec": spec.model_dump(mode="json"), "parameterization_completed": False,
        "md_system_ready": False}


def _openmm_system_preparation_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    required = {"parameterization_run_id", "parameterization_audit_sha256"}
    missing = sorted(required - set(arguments))
    if missing:
        raise ValueError(f"OpenMM system preparation missing arguments: {missing}")
    payload = {"parameterization_run_id": arguments["parameterization_run_id"],
        "parameterization_audit_sha256": arguments["parameterization_audit_sha256"],
        "max_minimization_iterations": arguments.get("max_minimization_iterations", 100)}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    budget = ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes": 30, "gpu_count": 0, "cpu_count": 2,
        "memory_gb": 16, "max_candidates": 1})
    return JobSubmissionRequest(request_id=f"openmm-system-preparation-{identity}",
        project_id=str(arguments["parameterization_run_id"]), route="library-first",
        engine="library-first-openmm-system-preparation", parameters=payload,
        resource_budget=budget, execution_mode=execution_mode,
        human_approved=bool(arguments.get("human_approved", False)),
        approval_note=arguments.get("approval_note"),
        parent_run_id=str(arguments["parameterization_run_id"]))


def library_first_prepare_openmm_system_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Compile, start or inspect conversion/audit/minimization for one CMS."""
    action = arguments.get("action", "plan")
    manager = JobManager(root)
    if action == "status":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("status requires run_id")
        spec = manager.registry.load_spec(run_id)
        if spec.engine != "library-first-openmm-system-preparation":
            raise ValueError("run_id is not an OpenMM system-preparation job")
        result = manager.registry.load_result(run_id)
        response = {"status": "PASS", "job": result.model_dump(mode="json"),
            "spec": spec.model_dump(mode="json"), "short_qc_completed": False,
            "md_system_ready": False}
        folder = manager.registry.job_dir(run_id) / "artifacts/openmm-system-preparation"
        files = {"preparation": folder / "openmm-preparation.json",
            "conversion": folder / "conversion/conversion.json",
            "forcefield_audit": folder / "forcefield-audit/forcefield-audit.json",
            "minimization": folder / "minimization/minimization.json"}
        for label, path in files.items():
            if path.is_file():
                response[label] = json.loads(path.read_text())
                response[label + "_sha256"] = sha256_file(path)
        return response
    if action not in {"plan", "start"}:
        raise ValueError("action must be plan, start, or status")
    request = _openmm_system_preparation_request(root, arguments,
        execution_mode="plan" if action == "plan" else "execute")
    if action == "plan":
        spec = manager.build_spec(request)
        return {"status": "PLANNED", "spec": spec.model_dump(mode="json"),
            "calculation_submitted": False, "short_qc_completed": False,
            "md_system_ready": False}
    result = manager.submit(request)
    spec = manager.registry.load_spec(result.run_id)
    return {"status": "QUEUED", "job": result.model_dump(mode="json"),
        "spec": spec.model_dump(mode="json"), "short_qc_completed": False,
        "md_system_ready": False}


def _openmm_short_qc_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    required = {"system_preparation_run_id", "preparation_manifest_sha256"}
    missing = sorted(required - set(arguments))
    if missing:
        raise ValueError(f"OpenMM short-QC missing arguments: {missing}")
    payload = {"system_preparation_run_id": arguments["system_preparation_run_id"],
        "preparation_manifest_sha256": arguments["preparation_manifest_sha256"],
        "steps": arguments.get("steps", 1000),
        "report_interval": arguments.get("report_interval", 100),
        "velocity_seed": arguments.get("velocity_seed", 20261014)}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    budget = ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes": 30, "gpu_count": 0, "cpu_count": 2,
        "memory_gb": 16, "max_candidates": 1})
    return JobSubmissionRequest(request_id=f"openmm-short-qc-{identity}",
        project_id=str(arguments["system_preparation_run_id"]), route="library-first",
        engine="library-first-openmm-short-qc", parameters=payload,
        resource_budget=budget, execution_mode=execution_mode,
        human_approved=bool(arguments.get("human_approved", False)),
        approval_note=arguments.get("approval_note"),
        parent_run_id=str(arguments["system_preparation_run_id"]))


def library_first_run_openmm_short_qc_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Compile, start or inspect bounded unrestrained OpenMM short-QC."""
    action = arguments.get("action", "plan")
    manager = JobManager(root)
    if action == "status":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("status requires run_id")
        spec = manager.registry.load_spec(run_id)
        if spec.engine != "library-first-openmm-short-qc":
            raise ValueError("run_id is not an OpenMM short-QC job")
        result = manager.registry.load_result(run_id)
        response = {"status": "PASS", "job": result.model_dump(mode="json"),
            "spec": spec.model_dump(mode="json"), "equilibration_completed": False,
            "md_system_ready": False}
        manifest = manager.registry.job_dir(run_id) / "artifacts/openmm-short-qc/short-qc.json"
        if manifest.is_file():
            response["short_qc"] = json.loads(manifest.read_text())
            response["short_qc_sha256"] = sha256_file(manifest)
        return response
    if action not in {"plan", "start"}:
        raise ValueError("action must be plan, start, or status")
    request = _openmm_short_qc_request(root, arguments,
        execution_mode="plan" if action == "plan" else "execute")
    if action == "plan":
        spec = manager.build_spec(request)
        return {"status": "PLANNED", "spec": spec.model_dump(mode="json"),
            "calculation_submitted": False, "equilibration_completed": False,
            "md_system_ready": False}
    result = manager.submit(request)
    spec = manager.registry.load_spec(result.run_id)
    return {"status": "QUEUED", "job": result.model_dump(mode="json"),
        "spec": spec.model_dump(mode="json"), "equilibration_completed": False,
        "md_system_ready": False}


def _openmm_equilibration_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    required = {"short_qc_run_id", "short_qc_manifest_sha256"}
    missing = sorted(required-set(arguments))
    if missing:
        raise ValueError(f"OpenMM equilibration missing arguments: {missing}")
    payload = {"short_qc_run_id": arguments["short_qc_run_id"],
        "short_qc_manifest_sha256": arguments["short_qc_manifest_sha256"],
        "integrator_seed": arguments.get("integrator_seed", 20261015),
        "barostat_seed": arguments.get("barostat_seed", 20261016)}
    if arguments.get('workflow_id'):
        payload['workflow_id'] = arguments['workflow_id']
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    registered_platform = JobManager(root).equilibration_runtime(payload).get('platform')
    budget = ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes": 120 if registered_platform == "CUDA" else 720,
        "gpu_count": 1 if registered_platform == "CUDA" else 0,
        "cpu_count": 20 if registered_platform == "CUDA" else 4,
        "memory_gb": 200 if registered_platform == "CUDA" else 32, "max_candidates": 1})
    return JobSubmissionRequest(request_id=f"openmm-equilibration-{identity}",
        project_id=str(arguments["short_qc_run_id"]), route="library-first",
        engine="library-first-openmm-equilibration", parameters=payload,
        resource_budget=budget, execution_mode=execution_mode,
        human_approved=bool(arguments.get("human_approved", False)),
        approval_note=arguments.get("approval_note"),
        parent_run_id=str(arguments["short_qc_run_id"]))


def library_first_equilibrate_openmm_system_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Compile, start or inspect the registered 500 ps NPT equilibration gate."""
    action = arguments.get("action", "plan")
    manager = JobManager(root)
    if action == "status":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("status requires run_id")
        spec = manager.registry.load_spec(run_id)
        if spec.engine != "library-first-openmm-equilibration":
            raise ValueError("run_id is not an OpenMM equilibration job")
        result = manager.registry.load_result(run_id)
        response = {"status": "PASS", "job": result.model_dump(mode="json"),
            "spec": spec.model_dump(mode="json"), "production_started": False,
            "md_system_ready": False}
        manifest = manager.registry.job_dir(run_id)/"artifacts/openmm-equilibration/equilibration.json"
        if manifest.is_file():
            response["equilibration"] = json.loads(manifest.read_text())
            response["equilibration_sha256"] = sha256_file(manifest)
        return response
    if action not in {"plan", "start"}:
        raise ValueError("action must be plan, start, or status")
    request = _openmm_equilibration_request(root, arguments,
        execution_mode="plan" if action == "plan" else "execute")
    if action == "plan":
        spec = manager.build_spec(request)
        return {"status": "PLANNED", "spec": spec.model_dump(mode="json"),
            "calculation_submitted": False, "production_started": False,
            "md_system_ready": False}
    result = manager.submit(request)
    spec = manager.registry.load_spec(result.run_id)
    return {"status": "QUEUED", "job": result.model_dump(mode="json"),
        "spec": spec.model_dump(mode="json"), "production_started": False,
        "md_system_ready": False}


def _openmm_production_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    required={"equilibration_run_id","equilibration_manifest_sha256","replica"}
    missing=sorted(required-set(arguments))
    if missing: raise ValueError(f"OpenMM production missing arguments: {missing}")
    payload={"equilibration_run_id":arguments["equilibration_run_id"],
        "equilibration_manifest_sha256":arguments["equilibration_manifest_sha256"],
        "replica":arguments["replica"],"seed_base":arguments.get("seed_base",20261020)}
    identity=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:20]
    platform=JobManager(root).config.get("openmm_production",{}).get("platform")
    budget=ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes":1440,"gpu_count":1 if platform=="CUDA" else 0,
        "cpu_count":4,"memory_gb":32,"max_candidates":1})
    return JobSubmissionRequest(request_id=f"openmm-production-{identity}",
        project_id=str(arguments["equilibration_run_id"]),route="library-first",
        engine="library-first-openmm-production",parameters=payload,resource_budget=budget,
        execution_mode=execution_mode,human_approved=bool(arguments.get("human_approved",False)),
        approval_note=arguments.get("approval_note"),parent_run_id=str(arguments["equilibration_run_id"]))


def library_first_run_openmm_production_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    action=arguments.get("action","plan"); manager=JobManager(root)
    if action=="status":
        run_id=arguments.get("run_id")
        if not isinstance(run_id,str): raise ValueError("status requires run_id")
        spec=manager.registry.load_spec(run_id)
        if spec.engine!="library-first-openmm-production": raise ValueError("run_id is not an OpenMM production job")
        result=manager.registry.load_result(run_id)
        response={"status":"PASS","job":result.model_dump(mode="json"),
            "spec":spec.model_dump(mode="json"),"trajectory_audited":False,"analysis_completed":False}
        manifest=manager.registry.job_dir(run_id)/"artifacts/openmm-production/production.json"
        if manifest.is_file():
            response["production"]=json.loads(manifest.read_text()); response["production_sha256"]=sha256_file(manifest)
        return response
    if action not in {"plan","start"}: raise ValueError("action must be plan, start, or status")
    request=_openmm_production_request(root,arguments,execution_mode="plan" if action=="plan" else "execute")
    if action=="plan":
        spec=manager.build_spec(request)
        return {"status":"PLANNED","spec":spec.model_dump(mode="json"),
            "calculation_submitted":False,"trajectory_audited":False,"analysis_completed":False}
    result=manager.submit(request); spec=manager.registry.load_spec(result.run_id)
    return {"status":"QUEUED","job":result.model_dump(mode="json"),
        "spec":spec.model_dump(mode="json"),"trajectory_audited":False,"analysis_completed":False}


def _trajectory_audit_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    required = {"production_run_id", "production_manifest_sha256"}
    missing = sorted(required - set(arguments))
    if missing:
        raise ValueError(f"Trajectory audit missing arguments: {missing}")
    payload = {"production_run_id": arguments["production_run_id"],
        "production_manifest_sha256": arguments["production_manifest_sha256"]}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    budget = ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes": 120, "gpu_count": 0, "cpu_count": 4,
        "memory_gb": 16, "max_candidates": 1})
    return JobSubmissionRequest(request_id=f"trajectory-audit-{identity}",
        project_id=str(arguments["production_run_id"]), route="library-first",
        engine="library-first-trajectory-audit", parameters=payload,
        resource_budget=budget, execution_mode=execution_mode,
        human_approved=bool(arguments.get("human_approved", False)),
        approval_note=arguments.get("approval_note"),
        parent_run_id=str(arguments["production_run_id"]))


def library_first_audit_openmm_trajectory_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    action = arguments.get("action", "plan")
    manager = JobManager(root)
    if action == "status":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("status requires run_id")
        spec = manager.registry.load_spec(run_id)
        if spec.engine != "library-first-trajectory-audit":
            raise ValueError("run_id is not a trajectory-audit job")
        result = manager.registry.load_result(run_id)
        response = {"status": "PASS", "job": result.model_dump(mode="json"),
            "spec": spec.model_dump(mode="json"), "structural_analysis_completed": False}
        manifest = manager.registry.job_dir(run_id) / "artifacts/trajectory-audit/trajectory-audit.json"
        if manifest.is_file():
            response["trajectory_audit"] = json.loads(manifest.read_text())
            response["trajectory_audit_sha256"] = sha256_file(manifest)
        return response
    if action not in {"plan", "start"}:
        raise ValueError("action must be plan, start, or status")
    request = _trajectory_audit_request(root, arguments,
        execution_mode="plan" if action == "plan" else "execute")
    if action == "plan":
        spec = manager.build_spec(request)
        return {"status": "PLANNED", "spec": spec.model_dump(mode="json"),
            "calculation_submitted": False, "structural_analysis_completed": False}
    result = manager.submit(request)
    spec = manager.registry.load_spec(result.run_id)
    return {"status": "QUEUED", "job": result.model_dump(mode="json"),
        "spec": spec.model_dump(mode="json"), "structural_analysis_completed": False}


def _structural_analysis_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    parents = arguments.get("trajectory_audits")
    if not isinstance(parents, list) or len(parents) != 3:
        raise ValueError("Structural analysis requires exactly three trajectory audits")
    payload = {"trajectory_audits": parents}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    budget = ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes": 240, "gpu_count": 0, "cpu_count": 8,
        "memory_gb": 32, "max_candidates": 1})
    return JobSubmissionRequest(request_id=f"structural-analysis-{identity}",
        project_id="three-replica-" + identity, route="library-first",
        engine="library-first-structural-analysis", parameters=payload,
        resource_budget=budget, execution_mode=execution_mode,
        human_approved=bool(arguments.get("human_approved", False)),
        approval_note=arguments.get("approval_note"))


def library_first_analyze_openmm_replicas_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    action = arguments.get("action", "plan"); manager = JobManager(root)
    if action == "status":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str): raise ValueError("status requires run_id")
        spec = manager.registry.load_spec(run_id)
        if spec.engine != "library-first-structural-analysis":
            raise ValueError("run_id is not a structural-analysis job")
        result = manager.registry.load_result(run_id)
        response = {"status": "PASS", "job": result.model_dump(mode="json"),
            "spec": spec.model_dump(mode="json"), "screening_decision_made": False}
        manifest = manager.registry.job_dir(run_id) / "artifacts/structural-analysis/structural-analysis.json"
        if manifest.is_file():
            response["structural_analysis"] = json.loads(manifest.read_text())
            response["structural_analysis_sha256"] = sha256_file(manifest)
        return response
    if action not in {"plan", "start"}: raise ValueError("action must be plan, start, or status")
    request = _structural_analysis_request(root, arguments,
        execution_mode="plan" if action == "plan" else "execute")
    if action == "plan":
        spec = manager.build_spec(request)
        return {"status": "PLANNED", "spec": spec.model_dump(mode="json"),
            "calculation_submitted": False, "screening_decision_made": False}
    result = manager.submit(request); spec = manager.registry.load_spec(result.run_id)
    return {"status": "QUEUED", "job": result.model_dump(mode="json"),
        "spec": spec.model_dump(mode="json"), "screening_decision_made": False}


def _md_report_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    required = {"structural_analysis_run_id", "structural_analysis_manifest_sha256"}
    missing = sorted(required - set(arguments))
    if missing: raise ValueError(f"MD report missing arguments: {missing}")
    payload = {key: arguments[key] for key in sorted(required)}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    budget = ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes":30,"gpu_count":0,"cpu_count":2,"memory_gb":4,"max_candidates":1})
    return JobSubmissionRequest(request_id=f"md-report-{identity}",
        project_id=str(arguments["structural_analysis_run_id"]), route="library-first",
        engine="library-first-md-report", parameters=payload, resource_budget=budget,
        execution_mode=execution_mode, human_approved=bool(arguments.get("human_approved",False)),
        approval_note=arguments.get("approval_note"),
        parent_run_id=str(arguments["structural_analysis_run_id"]))


def library_first_build_md_report_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    action=arguments.get("action","plan"); manager=JobManager(root)
    if action=="status":
        run_id=arguments.get("run_id")
        if not isinstance(run_id,str): raise ValueError("status requires run_id")
        spec=manager.registry.load_spec(run_id)
        if spec.engine!="library-first-md-report": raise ValueError("run_id is not an MD-report job")
        result=manager.registry.load_result(run_id)
        response={"status":"PASS","job":result.model_dump(mode="json"),
            "spec":spec.model_dump(mode="json"),"expert_review_required":True}
        manifest=manager.registry.job_dir(run_id)/"artifacts/md-report/candidate-md-assessment.json"
        report=manager.registry.job_dir(run_id)/"artifacts/md-report/candidate-md-report.md"
        if manifest.is_file():
            response["assessment"]=json.loads(manifest.read_text()); response["assessment_sha256"]=sha256_file(manifest)
        if report.is_file(): response["report_path"]=str(report)
        return response
    if action not in {"plan","start"}: raise ValueError("action must be plan, start, or status")
    request=_md_report_request(root,arguments,execution_mode="plan" if action=="plan" else "execute")
    if action=="plan":
        spec=manager.build_spec(request)
        return {"status":"PLANNED","spec":spec.model_dump(mode="json"),
            "calculation_submitted":False,"expert_review_required":True}
    result=manager.submit(request); spec=manager.registry.load_spec(result.run_id)
    return {"status":"QUEUED","job":result.model_dump(mode="json"),
        "spec":spec.model_dump(mode="json"),"expert_review_required":True}


def _md_candidate_delivery_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    required = {"pareto_run_id", "pareto_manifest_sha256"}
    missing = sorted(required - set(arguments))
    if missing:
        raise ValueError(f"MD candidate delivery missing arguments: {missing}")
    payload = {key: arguments[key] for key in sorted(required)}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    budget = ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes":30,"gpu_count":0,"cpu_count":2,"memory_gb":8,
        "max_candidates":100})
    return JobSubmissionRequest(request_id=f"md-candidate-delivery-{identity}",
        project_id=str(arguments["pareto_run_id"]), route="library-first",
        engine="library-first-md-candidate-delivery", parameters=payload,
        resource_budget=budget, execution_mode=execution_mode,
        human_approved=bool(arguments.get("human_approved",False)),
        approval_note=arguments.get("approval_note"),
        parent_run_id=str(arguments["pareto_run_id"]))


def library_first_deliver_md_candidates_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    action=arguments.get("action","plan"); manager=JobManager(root)
    if action=="status":
        run_id=arguments.get("run_id")
        if not isinstance(run_id,str): raise ValueError("status requires run_id")
        spec=manager.registry.load_spec(run_id)
        if spec.engine!="library-first-md-candidate-delivery":
            raise ValueError("run_id is not an MD-candidate-delivery job")
        result=manager.registry.load_result(run_id)
        response={"status":"PASS","job":result.model_dump(mode="json"),
            "spec":spec.model_dump(mode="json"),"candidate_selected_for_md":False,
            "native_graph_qc_completed":False}
        manifest=manager.registry.job_dir(run_id)/"artifacts/md-candidate-delivery/candidate-results.json"
        if manifest.is_file():
            response["candidate_delivery"]=json.loads(manifest.read_text())
            response["candidate_delivery_sha256"]=sha256_file(manifest)
        return response
    if action not in {"plan","start"}: raise ValueError("action must be plan, start, or status")
    request=_md_candidate_delivery_request(root,arguments,
        execution_mode="plan" if action=="plan" else "execute")
    if action=="plan":
        spec=manager.build_spec(request)
        return {"status":"PLANNED","spec":spec.model_dump(mode="json"),
            "calculation_submitted":False,"candidate_selected_for_md":False,
            "native_graph_qc_completed":False}
    result=manager.submit(request); spec=manager.registry.load_spec(result.run_id)
    return {"status":"QUEUED","job":result.model_dump(mode="json"),
        "spec":spec.model_dump(mode="json"),"candidate_selected_for_md":False,
        "native_graph_qc_completed":False}


def library_first_select_md_candidate_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Bind one frozen candidate to registered receptor parents for native graph QC."""
    from covscreen.md_candidate_pose import (
        freeze_delivered_candidate_pose, load_candidate_pose_input)
    required={"delivery_run_id","delivery_manifest_sha256","compound_id",
        "input_sdf_index","preparation_run_id","prepared_receptor_manifest_sha256",
        "receptor_context_artifact_id","receptor_context_manifest_sha256"}
    missing=sorted(required-set(arguments))
    if missing: raise ValueError(f"MD candidate selection missing arguments: {missing}")
    action=arguments.get("action","plan")
    if action not in {"plan","select"}: raise ValueError("action must be plan or select")
    root=Path(root).resolve(); manager=JobManager(root)
    delivery_run=str(arguments["delivery_run_id"])
    delivery_spec=manager.registry.load_spec(delivery_run)
    delivery_result=manager.registry.load_result(delivery_run)
    delivery=manager.registry.job_dir(delivery_run)/"artifacts/md-candidate-delivery/candidate-results.json"
    if (delivery_spec.engine!="library-first-md-candidate-delivery"
            or delivery_result.state!="COMPLETED" or delivery_result.return_code!=0
            or delivery.is_symlink() or not delivery.is_file()
            or sha256_file(delivery)!=arguments["delivery_manifest_sha256"]):
        raise ValueError("A completed exact MD candidate delivery is required")
    preparation_run=str(arguments["preparation_run_id"])
    preparation_spec=manager.registry.load_spec(preparation_run)
    preparation_result=manager.registry.load_result(preparation_run)
    prepared=manager.registry.job_dir(preparation_run)/"artifacts/receptor-preparation/prepared/prepared-receptor.json"
    if (preparation_spec.engine!="library-first-receptor-preparation"
            or preparation_result.state!="COMPLETED" or preparation_result.return_code!=0
            or prepared.is_symlink() or not prepared.is_file()
            or sha256_file(prepared)!=arguments["prepared_receptor_manifest_sha256"]):
        raise ValueError("A completed exact prepared receptor is required")
    context_id=arguments["receptor_context_artifact_id"]
    if not isinstance(context_id,str) or not re.fullmatch(r"receptor-context-[a-f0-9]{32}",context_id):
        raise ValueError("Use a receptor-context artifact ID")
    context=(root/"state/receptor_site_context"/context_id[17:]/"site-context.json").resolve()
    context.relative_to(root)
    if (context.is_symlink() or not context.is_file()
            or sha256_file(context)!=arguments["receptor_context_manifest_sha256"]):
        raise ValueError("Exact receptor-context artifact is unavailable")
    values=(delivery,arguments["delivery_manifest_sha256"],arguments["compound_id"],
        arguments["input_sdf_index"],prepared,arguments["prepared_receptor_manifest_sha256"],
        context,arguments["receptor_context_manifest_sha256"])
    identity=hashlib.sha256(json.dumps({key:arguments[key] for key in sorted(required)},
        sort_keys=True,separators=(",",":")).encode()).hexdigest()[:32]
    artifact_id="candidate-pose-input-"+identity
    if action=="plan":
        with tempfile.TemporaryDirectory(prefix="candidate-pose-plan-") as temporary:
            manifest=freeze_delivered_candidate_pose(*values,Path(temporary)/"artifact")
        return {"status":"PLANNED","candidate_id":manifest["candidate_id"],
            "compound_id":manifest["compound_id"],"input_sdf_index":manifest["input_sdf_index"],
            "candidate_pose_artifact_id":artifact_id,"native_graph_qc_completed":False,
            "calculation_submitted":False}
    output=root/"state/candidate_pose_input"/identity
    if output.exists():
        path=output/"candidate-pose-input.json"
        loaded=load_candidate_pose_input(root,artifact_id,sha256_file(path))
        manifest=loaded["manifest"]
        if (manifest["compound_id"]!=arguments["compound_id"]
                or manifest["input_sdf_index"]!=arguments["input_sdf_index"]
                or manifest["parents"]["candidate_delivery_sha256"]
                   !=arguments["delivery_manifest_sha256"]):
            raise ValueError("Existing candidate selection differs from requested parents")
    else:
        manifest=freeze_delivered_candidate_pose(*values,output)
    path=output/"candidate-pose-input.json"
    return {"status":"PASS","artifact_id":artifact_id,
        "manifest_sha256":sha256_file(path),"candidate_id":manifest["candidate_id"],
        "compound_id":manifest["compound_id"],"input_sdf_index":manifest["input_sdf_index"],
        "native_graph_qc_completed":False,"calculation_submitted":False}


def _md_adduct_admission_request(root: Path, arguments: dict[str, Any], *, execution_mode: str):
    required={"candidate_pose_artifact_id","manifest_sha256"}
    missing=sorted(required-set(arguments))
    if missing: raise ValueError(f"MD adduct admission missing arguments: {missing}")
    payload={"candidate_pose_artifact_id":arguments["candidate_pose_artifact_id"],
        "manifest_sha256":arguments["manifest_sha256"],
        "native_timeout_seconds":arguments.get("native_timeout_seconds",1200)}
    identity=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:20]
    budget=ResourceBudget.model_validate(arguments.get("resource_budget") or {
        "max_runtime_minutes":60,"gpu_count":0,"cpu_count":4,"memory_gb":16,
        "max_candidates":1})
    return JobSubmissionRequest(request_id=f"md-adduct-admission-{identity}",
        project_id=str(arguments["candidate_pose_artifact_id"]),route="library-first",
        engine="library-first-md-adduct-admission",parameters=payload,
        resource_budget=budget,execution_mode=execution_mode,
        human_approved=bool(arguments.get("human_approved",False)),
        approval_note=arguments.get("approval_note"))


def library_first_admit_md_candidate_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    action=arguments.get("action","plan"); manager=JobManager(root)
    if action=="status":
        run_id=arguments.get("run_id")
        if not isinstance(run_id,str): raise ValueError("status requires run_id")
        spec=manager.registry.load_spec(run_id)
        if spec.engine!="library-first-md-adduct-admission":
            raise ValueError("run_id is not an MD-adduct-admission job")
        result=manager.registry.load_result(run_id)
        response={"status":"PASS","job":result.model_dump(mode="json"),
            "spec":spec.model_dump(mode="json"),"parameterization_completed":False,
            "md_system_ready":False}
        folder=manager.registry.job_dir(run_id)/"artifacts/md-adduct-admission"
        for label,path in {"admission":folder/"md-adduct-admission.json",
                "adduct_input":folder/"adduct-input/adduct-input.json"}.items():
            if path.is_file():
                response[label]=json.loads(path.read_text()); response[label+"_sha256"]=sha256_file(path)
        return response
    if action not in {"plan","start"}: raise ValueError("action must be plan, start, or status")
    request=_md_adduct_admission_request(root,arguments,
        execution_mode="plan" if action=="plan" else "execute")
    if action=="plan":
        spec=manager.build_spec(request)
        return {"status":"PLANNED","spec":spec.model_dump(mode="json"),
            "calculation_submitted":False,"native_graph_qc_completed":False,
            "parameterization_completed":False,"md_system_ready":False}
    result=manager.submit(request); spec=manager.registry.load_spec(result.run_id)
    return {"status":"QUEUED","job":result.model_dump(mode="json"),
        "spec":spec.model_dump(mode="json"),"native_graph_qc_completed":False,
        "parameterization_completed":False,"md_system_ready":False}


def library_first_next_md_action_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate one formal MD stage and compile, but never submit, its next action."""
    stage=arguments.get("stage")
    allowed={"candidate_delivery","candidate_pose","adduct_admission","parameterization",
        "system_preparation","short_qc","equilibration","production_replicas",
        "trajectory_audits","structural_analysis","md_report"}
    if stage not in allowed: raise ValueError("Unsupported Library-first MD stage")
    def require(*names):
        missing=[name for name in names if name not in arguments]
        if missing: raise ValueError(f"{stage} next-action evidence is missing: {missing}")
    if stage=="candidate_delivery":
        names=("delivery_run_id","delivery_manifest_sha256","compound_id","input_sdf_index",
            "preparation_run_id","prepared_receptor_manifest_sha256",
            "receptor_context_artifact_id","receptor_context_manifest_sha256")
        require(*names); payload={name:arguments[name] for name in names}; payload["action"]="plan"
        plan=library_first_select_md_candidate_tool(root,payload)
        return {"status":"NEXT_ACTION_PLANNED","current_stage":stage,
            "next_tool":"covalent_library_first_select_md_candidate",
            "next_arguments":{**payload,"action":"select"},"validated_plan":plan,
            "calculation_submitted":False}
    if stage=="candidate_pose":
        require("candidate_pose_artifact_id","manifest_sha256")
        payload={"action":"plan","candidate_pose_artifact_id":arguments["candidate_pose_artifact_id"],
            "manifest_sha256":arguments["manifest_sha256"]}
        plan=library_first_admit_md_candidate_tool(root,payload)
        return {"status":"NEXT_ACTION_PLANNED","current_stage":stage,
            "next_tool":"covalent_library_first_admit_md_candidate",
            "next_arguments":payload,"validated_plan":plan,
            "execution_action":"start_requires_native_approval","calculation_submitted":False}
    if stage=="adduct_admission":
        require("adduct_admission_run_id","adduct_input_manifest_sha256")
        payload={"action":"plan","adduct_admission_run_id":arguments["adduct_admission_run_id"],
            "adduct_input_manifest_sha256":arguments["adduct_input_manifest_sha256"]}
        plan=library_first_parameterize_adduct_tool(root,payload)
        return {"status":"NEXT_ACTION_PLANNED","current_stage":stage,
            "next_tool":"covalent_library_first_parameterize_adduct",
            "next_arguments":payload,"validated_plan":plan,
            "execution_action":"start_requires_native_approval","calculation_submitted":False}
    if stage=="parameterization":
        require("parameterization_run_id","parameterization_audit_sha256")
        payload={"action":"plan","parameterization_run_id":arguments["parameterization_run_id"],
            "parameterization_audit_sha256":arguments["parameterization_audit_sha256"]}
        plan=library_first_prepare_openmm_system_tool(root,payload)
        tool="covalent_library_first_prepare_openmm_system"
    elif stage=="system_preparation":
        require("system_preparation_run_id","preparation_manifest_sha256")
        payload={"action":"plan","system_preparation_run_id":arguments["system_preparation_run_id"],
            "preparation_manifest_sha256":arguments["preparation_manifest_sha256"]}
        plan=library_first_run_openmm_short_qc_tool(root,payload)
        tool="covalent_library_first_run_openmm_short_qc"
    elif stage=="short_qc":
        require("short_qc_run_id","short_qc_manifest_sha256")
        payload={"action":"plan","short_qc_run_id":arguments["short_qc_run_id"],
            "short_qc_manifest_sha256":arguments["short_qc_manifest_sha256"]}
        for name in ("integrator_seed","barostat_seed"):
            if name in arguments: payload[name]=arguments[name]
        plan=library_first_equilibrate_openmm_system_tool(root,payload)
        tool="covalent_library_first_equilibrate_openmm_system"
    elif stage=="equilibration":
        require("equilibration_run_id","equilibration_manifest_sha256")
        calls=[]
        for replica in (1,2,3):
            payload={"action":"plan","equilibration_run_id":arguments["equilibration_run_id"],
                "equilibration_manifest_sha256":arguments["equilibration_manifest_sha256"],
                "replica":replica}
            if "seed_base" in arguments: payload["seed_base"]=arguments["seed_base"]
            calls.append({"tool":"covalent_library_first_run_openmm_production",
                "arguments":payload,"execution_action":"start_requires_native_approval",
                "validated_plan":library_first_run_openmm_production_tool(root,payload)})
        return {"status":"NEXT_ACTIONS_PLANNED","current_stage":stage,
            "next_calls":calls,"all_replicas_required":True,"calculation_submitted":False}
    elif stage=="production_replicas":
        replicas=arguments.get("production_replicas")
        if not isinstance(replicas,list) or len(replicas)!=3:
            raise ValueError("production_replicas requires exactly three run/hash records")
        calls=[]
        for item in replicas:
            if not isinstance(item,dict) or set(item)!={"run_id","manifest_sha256"}:
                raise ValueError("Each production replica requires only run_id and manifest_sha256")
            payload={"action":"plan","production_run_id":item["run_id"],
                "production_manifest_sha256":item["manifest_sha256"]}
            calls.append({"tool":"covalent_library_first_audit_openmm_trajectory",
                "arguments":payload,"execution_action":"start_requires_native_approval",
                "validated_plan":library_first_audit_openmm_trajectory_tool(root,payload)})
        return {"status":"NEXT_ACTIONS_PLANNED","current_stage":stage,
            "next_calls":calls,"all_replicas_required":True,"calculation_submitted":False}
    elif stage=="trajectory_audits":
        audits=arguments.get("trajectory_audits")
        if not isinstance(audits,list) or len(audits)!=3:
            raise ValueError("trajectory_audits requires exactly three run/hash records")
        payload={"action":"plan","trajectory_audits":audits}
        plan=library_first_analyze_openmm_replicas_tool(root,payload)
        tool="covalent_library_first_analyze_openmm_replicas"
    elif stage=="structural_analysis":
        require("structural_analysis_run_id","structural_analysis_manifest_sha256")
        payload={"action":"plan","structural_analysis_run_id":arguments["structural_analysis_run_id"],
            "structural_analysis_manifest_sha256":arguments["structural_analysis_manifest_sha256"]}
        plan=library_first_build_md_report_tool(root,payload)
        tool="covalent_library_first_build_md_report"
    else:
        require("md_report_run_id","md_report_manifest_sha256")
        manager=JobManager(root); run_id=str(arguments["md_report_run_id"])
        spec=manager.registry.load_spec(run_id); result=manager.registry.load_result(run_id)
        manifest=manager.registry.job_dir(run_id)/"artifacts/md-report/candidate-md-assessment.json"
        if (spec.engine!="library-first-md-report" or result.state!="COMPLETED"
                or result.return_code!=0 or manifest.is_symlink() or not manifest.is_file()
                or sha256_file(manifest)!=arguments["md_report_manifest_sha256"]):
            raise ValueError("A completed exact candidate MD report is required")
        assessment=json.loads(manifest.read_text())
        return {"status":"MD_WORKFLOW_COMPLETE_EXPERT_REVIEW_REQUIRED",
            "current_stage":stage,"assessment":assessment,
            "assessment_sha256":sha256_file(manifest),"expert_review_required":True,
            "calculation_submitted":False}
    return {"status":"NEXT_ACTION_PLANNED","current_stage":stage,"next_tool":tool,
        "next_arguments":payload,"validated_plan":plan,
        "execution_action":"start_requires_native_approval","calculation_submitted":False}


def ligand_first_resolve_names_tool(
    root: Path, gene: str, taxon: int, cys: int, ligand: str, *, action: str = "plan",
) -> dict[str, Any]:
    if action not in {"plan", "search"}:
        raise ValueError("action must be plan or search")
    plan = identity_plan(gene, taxon, cys, ligand)
    if action == "plan":
        return {"status": "PASS", "plan": plan, "network_request_sent": False,
                "campaign_registered": False, "calculation_submitted": False}
    directory = resolve_within(root, Path("state/identity_resolution") / uuid.uuid4().hex)
    result = resolve_names(gene, taxon, cys, ligand, directory)
    response = {"status": "FAIL" if result["status"] == "lookup_failed" else "PASS",
                "resolution": result, "artifact_directory": str(directory.relative_to(root.resolve())),
                "campaign_registered": False, "calculation_submitted": False}
    if "structure_discovery_inputs" in result:
        ids = result["structure_discovery_inputs"]
        response["next_tool"] = {"name": "covalent_ligand_first_discover_structures",
            "arguments": {"uniprot": ids["uniprot_id"], "component": ids["component_id"], "action": "plan"}}
    return response


def ligand_first_prepare_inputs_tool(
    root: Path, identity_directory: str, discovery_directory: str, entry: str,
    noncovalent_parent_assertion: str, *, action: str = "plan",
    label_chain: str | None = None, ligand_chain: str | None = None,
    ligand_residue: str | None = None, model: int | None = None, altloc: str | None = None,
) -> dict[str, Any]:
    """Consume saved lookup evidence, not retyped chemical/protein identities."""
    if action not in {"plan", "prepare"}:
        raise ValueError("action must be plan or prepare")
    if not noncovalent_parent_assertion.strip():
        raise ValueError("Explicit caller noncovalent-parent assertion required")
    identity_root = resolve_within(root, identity_directory)
    discovery_root = resolve_within(root, discovery_directory)
    identity_path = identity_root / "identity_resolution.json"
    discovery_path = discovery_root / "structure_discovery.json"
    identity = json.loads(identity_path.read_text())
    discovery = json.loads(discovery_path.read_text())
    if identity.get("status") != "identifiers_resolved_structure_validation_required":
        raise ValueError("Unique verified identity evidence required")
    expected_sources = {"uniprot", "pubchem", "rcsb_ccd"}
    evidence = identity.get("evidence", [])
    if len(evidence) != 3 or {item.get("source") for item in evidence} != expected_sources:
        raise ValueError("Complete identity source evidence required")
    for item in evidence:
        if sha256_file(identity_root / (item["source"] + ".json")) != item["sha256"]:
            raise ValueError("Identity raw response hash mismatch")
    if sha256_file(discovery_root / "rcsb_response.json") != discovery["raw_response_sha256"]:
        raise ValueError("Structure discovery raw response hash mismatch")
    ids = identity["structure_discovery_inputs"]
    query = discovery["query"]
    limit = query["request_options"]["paginate"]["rows"]
    if query != build_query(ids["uniprot_id"], ids["component_id"], limit):
        raise ValueError("Structure discovery is for a different target/parent")
    raw_discovery = json.loads((discovery_root / "rcsb_response.json").read_text())
    if entry not in {row.get("identifier") for row in raw_discovery.get("result_set", [])}:
        raise ValueError("Selected entry is absent from frozen discovery response")
    plan = identity["plan"]
    acquisition = acquisition_plan(entry, ids["uniprot_id"], plan["cys"], ids["component_id"])
    selection = dict(label_chain=label_chain, ligand_chain=ligand_chain,
                     ligand_residue=ligand_residue, model=model, altloc=altloc)
    result = {"status": "PASS", "action": action, "acquisition_plan": {**acquisition, "max_calls": 27},
              "identity_manifest_sha256": sha256_file(identity_path),
              "discovery_manifest_sha256": sha256_file(discovery_path),
              "selection": selection, "runtime_registered": False, "calculation_submitted": False}
    if action == "plan":
        return {**result, "network_request_sent": False}
    directory = resolve_within(root, Path("state/native_cases") / uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    acquired = acquire_structure(entry, ids["uniprot_id"], plan["cys"], ids["component_id"],
        directory / "acquisition", expected_inchikey=identity["compounds"][0]["inchikey"], **selection)
    if acquired["status"] != "acquired_coordinate_audit_completed_review_pending":
        return {**result, "status": "FAIL", "acquisition": acquired,
                "artifact_directory": str(directory.relative_to(root.resolve()))}
    prepared = prepare_native_case(directory / "acquisition", directory / "case",
        campaign_id="native-" + directory.name, gene=plan["gene"], taxon=plan["taxon"],
        ligand_name=plan["ligand_name"], noncovalent_parent_assertion=noncovalent_parent_assertion)
    result.update(preparation=prepared, artifact_directory=str(directory.relative_to(root.resolve())))
    (directory / "preparation_manifest.json").write_text(json.dumps(result, indent=2))
    return result


def ligand_first_audit_structure_site_tool(
    root: Path, pdb_path: str, mapping_path: str, component: str, *,
    ligand_chain: str | None = None, ligand_residue: str | None = None,
    model: int | None = None, altloc: str | None = None,
) -> dict[str, Any]:
    """Read registered/local structure artifacts; never launch a calculation."""
    structure = resolve_within(root, pdb_path)
    mapping_file = resolve_within(root, mapping_path)
    raw = mapping_file.read_bytes()
    result = audit_structure_site(structure, json.loads(raw), component,
        ligand_chain=ligand_chain, ligand_residue=ligand_residue, model=model, altloc=altloc)
    result["mapping_sha256"] = hashlib.sha256(raw).hexdigest()
    return {"status": "PASS", "audit": result, "calculation_submitted": False}


def ligand_first_discover_structures_tool(
    root: Path, uniprot: str, component: str, *, limit: int = 20, action: str = "plan"
) -> dict[str, Any]:
    """Expose bounded public metadata lookup without accepting shell/URL/path input."""
    if action not in {"plan", "search"}:
        raise ValueError("action must be plan or search")
    query = build_query(uniprot, component, limit)
    if action == "plan":
        return {"status": "PASS", "action": "plan", "endpoint": RCSB_SEARCH_ENDPOINT,
                "query": query, "max_calls": 1, "max_records": limit,
                "network_request_sent": False, "campaign_registered": False}
    directory = resolve_within(root, Path("state/structure_discovery") / uuid.uuid4().hex)
    result = discover(uniprot, component, limit, directory)
    return {"status": "PASS", "action": "search", "discovery": result,
            "artifact_directory": str(directory.relative_to(root.resolve())),
            "campaign_registered": False, "calculation_submitted": False}


def ligand_first_chain_tool(
    root: Path,
    config_path: str,
    *,
    execution_mode: str = "plan",
    submit: bool = False,
    human_approved: bool = False,
    approval_note: str | None = None,
) -> dict[str, Any]:
    """Inspect and optionally submit the sole legal next Ligand-first stage."""

    if execution_mode not in {"plan", "execute"}:
        raise ValueError("execution_mode must be plan or execute")
    config_abs = resolve_route_config(root, config_path)
    config = load_ligand_chain_config(config_abs)
    status = inspect_ligand_chain(root, config)
    request = compile_next_ligand_request(
        root,
        config,
        status,
        execution_mode=execution_mode,  # type: ignore[arg-type]
        human_approved=human_approved,
        approval_note=approval_note,
    )
    response: dict[str, Any] = {
        "status": "PASS",
        "chain": status.model_dump(mode="json"),
        "next_request": request.model_dump(mode="json") if request else None,
        "submitted": False,
    }
    if submit:
        if request is None:
            raise ValueError("the Ligand-first chain has no ready next stage")
        manager = JobManager(root)
        result = manager.submit(request)
        response.update(
            {
                "submitted": True,
                "job": result.model_dump(mode="json"),
                "spec": manager.registry.load_spec(result.run_id).model_dump(mode="json"),
            }
        )
    return response


def ligand_first_campaign_tool(
    root: Path, config_path: str, *, action: str = "plan",
    max_runtime_minutes: int = 240, human_approved: bool = False,
    approval_note: str | None = None, approved_plan_sha256: str | None = None,
    controller_id: str | None = None,
) -> dict[str, Any]:
    """Plan/start/status a bounded campaign supervisor, not a resource provisioner."""
    if action not in {"plan", "start", "status"}:
        raise ValueError("Invalid campaign action")
    if action == "status":
        if not controller_id or not all(c in "0123456789abcdef" for c in controller_id) or len(controller_id) != 32:
            raise ValueError("Invalid controller ID")
        controller_dir = resolve_within(root, f"state/ligand_controllers/runs/{controller_id}")
        state = controller_dir / "campaign_controller.json"
        if not state.is_file():
            return {"status": "NO_CONTROLLER_STATE", "controller_id": controller_id,
                    "message": "Inspect launch metadata and controller.log; liveness is not established."}
        persisted = json.loads(state.read_text())
        approved_path = controller_dir / "approved_plan.json"
        if not approved_path.is_file():
            return {
                **persisted,
                "status": "CONTROLLER_EVIDENCE_INCOMPLETE",
                "controller_id": controller_id,
                "status_is_persisted": True,
                "live_registry_observed": False,
                "failure": {
                    "stage": None,
                    "reason": "approved_plan_missing",
                },
            }
        approved = json.loads(approved_path.read_text())
        plan = approved.get("plan")
        expected_digest = approved.get("plan_sha256")
        observed_digest = (
            hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
            if isinstance(plan, dict)
            else None
        )
        if observed_digest != expected_digest:
            return {
                **persisted,
                "status": "CONTROLLER_EVIDENCE_INVALID",
                "controller_id": controller_id,
                "status_is_persisted": True,
                "live_registry_observed": False,
                "failure": {"stage": None, "reason": "approved_plan_digest_mismatch"},
            }
        try:
            config_abs = resolve_route_config(root, plan["config_path"])
            config = load_ligand_chain_config(config_abs)
            run_ids = dict(persisted.get("run_ids") or config.seed_run_ids)
            live_config = config.model_copy(update={"seed_run_ids": run_ids})
            observed = inspect_ligand_chain(root, live_config)
            observed_payload = observed.model_dump(mode="json")
            tracked = {
                item.stage: item.run_id
                for item in observed.stages
                if item.run_id is not None
            }
            untracked = {
                stage: run_id
                for stage, run_id in tracked.items()
                if run_ids.get(stage) != run_id
            }
            failed_stage = next(
                (
                    item
                    for item in observed.stages
                    if item.state in {"FAILED", "CANCELLED", "TIMED_OUT"}
                    or item.quality_pass is False
                ),
                None,
            )
            failure = (
                {"stage": failed_stage.stage, "run_id": failed_stage.run_id,
                 "reason": failed_stage.reason}
                if failed_stage
                else None
            )
            inputs_unchanged = bool(
                config_abs.is_file()
                and sha256_file(config_abs) == plan.get("config_sha256")
                and resolve_within(root, config.campaign_path).is_file()
                and sha256_file(resolve_within(root, config.campaign_path))
                == plan.get("campaign_sha256")
            )
        except Exception as exc:
            return {
                **persisted,
                "status": "CONTROLLER_EVIDENCE_INVALID",
                "controller_id": controller_id,
                "status_is_persisted": True,
                "live_registry_observed": False,
                "failure": {
                    "stage": None,
                    "reason": f"live_status_error:{type(exc).__name__}:{exc}",
                },
            }
        launch_path = controller_dir / "launch.json"
        controller_process = "unknown"
        if launch_path.is_file():
            try:
                pid = int(json.loads(launch_path.read_text())["pid"])
                command_line = Path(f"/proc/{pid}/cmdline")
                if command_line.is_file():
                    command = command_line.read_bytes().replace(b"\0", b" ").decode(errors="replace")
                    controller_process = (
                        "running"
                        if "run_ligand_first_campaign.py" in command
                        and str(controller_dir) in command
                        else "not_running"
                    )
                elif os.name == "posix" and Path("/proc").is_dir():
                    controller_process = "not_running"
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                controller_process = "unknown"
        if untracked:
            status_value = "REQUIRES_RECONCILIATION"
            failure = {"stage": next(iter(untracked)), "reason": "registered_run_not_in_controller_state",
                       "untracked_runs": untracked}
        elif not inputs_unchanged:
            status_value = "APPROVED_INPUTS_CHANGED"
            failure = {"stage": None, "reason": "approved_config_or_campaign_changed"}
        elif failure:
            status_value = "REQUIRES_REVIEW"
        else:
            status_value = observed.terminal_state.upper()
        return {
            **persisted,
            "status": status_value,
            "controller_id": controller_id,
            "status_is_persisted": True,
            "live_registry_observed": True,
            "controller_process": controller_process,
            "approved_inputs_unchanged": inputs_unchanged,
            "observed_chain": observed_payload,
            "failure": failure,
            "duplicate_submission_allowed": False,
        }
    if not 1 <= max_runtime_minutes <= 10080:
        raise ValueError("Invalid total campaign runtime budget")
    config_abs = resolve_route_config(root, config_path)
    config = load_ligand_chain_config(config_abs)
    campaign_abs = resolve_within(root, config.campaign_path)
    enumeration_ceiling = (
        config.upstream_candidate_budget
        if config.enumeration_backend == "upstream-native"
        else config.max_sites * 12
    )
    candidate_ceilings = {
        "structure_intake": 1,
        "covalentize": enumeration_ceiling,
        "carsidock": enumeration_ceiling,
        "carsidock_selection": min(config.carsidock_top_n, enumeration_ceiling),
        "af3": min(config.carsidock_top_n, enumeration_ceiling),
        "covdock_lo": min(config.af3_top_n, config.carsidock_top_n, enumeration_ceiling),
        "independent_evaluation": min(config.af3_top_n, config.carsidock_top_n, enumeration_ceiling),
        "pareto": min(config.af3_top_n, config.carsidock_top_n, enumeration_ceiling),
    }
    # Seeded stages are immutable evidence adopted by a continuation.  They are
    # part of the scientific trace, but they must never be executable under the
    # new campaign approval.  Keeping the two sets explicit prevents a broad
    # approval for a continuation from silently authorizing an AF3/CovDock
    # resubmission after a refresh or controller restart.
    executable_stage_order = [
        stage for stage in config.stage_order if stage not in config.seed_run_ids
    ]
    stage_resource_ceilings = {
        "structure_intake": {"gpu_count": 0, "cpu_count": 1, "memory_gb": 4},
        "covalentize": {"gpu_count": 0, "cpu_count": 2, "memory_gb": 8},
        "carsidock": {"gpu_count": 1, "cpu_count": 4, "memory_gb": 64},
        "carsidock_selection": {"gpu_count": 0, "cpu_count": 4, "memory_gb": 16},
        "af3": {"gpu_count": 1, "cpu_count": 4, "memory_gb": 96},
        "covdock_lo": {
            "gpu_count": 0,
            "cpu_count": config.covdock_cpu_count,
            "memory_gb": 128,
        },
        "independent_evaluation": {
            "gpu_count": 0,
            "cpu_count": 4,
            "memory_gb": 16,
        },
        "pareto": {"gpu_count": 0, "cpu_count": 2, "memory_gb": 16},
    }
    executable_resources = [stage_resource_ceilings[stage] for stage in executable_stage_order]
    peak = {
        name: max((int(resource[name]) for resource in executable_resources), default=0)
        for name in ("gpu_count", "cpu_count", "memory_gb")
    }
    plan = {
        "config_path": config_reference(root, config_abs),
        "config_sha256": sha256_file(config_abs),
        "campaign_sha256": sha256_file(campaign_abs),
        "campaign_id": config.campaign_id,
        "project_id": config.project_id,
        "route": "ligand-first",
        "stage_order": list(config.stage_order),
        "stage_engines": {stage: STAGE_ENGINES[stage] for stage in config.stage_order},
        "seed_run_ids": dict(config.seed_run_ids),
        "executable_stage_order": executable_stage_order,
        "executable_stage_engines": {
            stage: STAGE_ENGINES[stage] for stage in executable_stage_order
        },
        "candidate_ceiling_by_stage": {
            stage: candidate_ceilings[stage] for stage in config.stage_order
        },
        "resource_ceiling_by_stage": {
            stage: stage_resource_ceilings[stage] for stage in executable_stage_order
        },
        "enumeration_backend": config.enumeration_backend,
        "dispatch_policy": config.dispatch_policy,
        "dispatch_batch": config.dispatch_batch,
        "max_runtime_minutes": max_runtime_minutes,
        "peak_gpu_count": peak["gpu_count"],
        "peak_cpu_count": peak["cpu_count"],
        "peak_memory_gb": peak["memory_gb"],
        "enumeration_candidate_ceiling": enumeration_ceiling,
        "requires_configured_execution_host": True,
    }
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    if action == "plan":
        return {"status": "PLANNED", "plan": plan, "plan_sha256": digest}
    if not human_approved or not approval_note or not approval_note.strip() or approved_plan_sha256 != digest:
        raise ValueError("Starting requires explicit approval of this exact campaign plan and budget")
    from .execution import _worker_environment
    manager = JobManager(root)
    identifier = uuid.uuid4().hex
    out = root / "state/ligand_controllers/runs" / identifier
    out.mkdir(parents=True, exist_ok=False)
    (out / "approved_plan.json").write_text(json.dumps({"plan": plan, "plan_sha256": digest,
        "approval_note": approval_note,
        "native_approval": __import__('covscreen.native_approval', fromlist=['CURRENT']).CURRENT.get()}, ensure_ascii=False, indent=2) + "\n")
    command = [manager._python(), str(ligand_script(root, "run_ligand_first_campaign.py")),
        "--project-root", str(root), "--config", str(config_abs),
        "--approved-config-sha256", plan["config_sha256"],
        "--approved-campaign-sha256", plan["campaign_sha256"], "--human-approved",
        "--approval-note", approval_note, "--max-runtime-minutes", str(max_runtime_minutes),
        "--output-dir", str(out)]
    with (out / "controller.log").open("w") as log:
        process = subprocess.Popen(command, cwd=root, env=_worker_environment(root),
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    (out / "launch.json").write_text(json.dumps({"pid": process.pid, "controller_id": identifier}) + "\n")
    return {"status": "STARTING", "controller_id": identifier, "pid": process.pid,
            "plan_sha256": digest, "state_path": str((out / "campaign_controller.json").relative_to(root))}


def retry_job_tool(
    root: Path,
    run_id: str,
    *,
    human_approved: bool,
    approval_note: str,
    execution_mode: str = "execute",
) -> dict[str, Any]:
    result = JobManager(root).retry(
        run_id,
        human_approved=human_approved,
        approval_note=approval_note,
        execution_mode=execution_mode,
    )
    return {"status": "PASS", "job": result.model_dump(mode="json")}


def list_jobs_tool(root: Path, *, limit: int = 50) -> dict[str, Any]:
    results = JobManager(root).registry.list_results(limit=limit)
    return {
        "status": "PASS",
        "count": len(results),
        "jobs": [result.model_dump(mode="json") for result in results],
    }


def get_job_tool(root: Path, run_id: str) -> dict[str, Any]:
    manager = JobManager(root)
    spec = manager.registry.load_spec(run_id)
    result = manager.registry.load_result(run_id)
    _verify_observed_job_identity(run_id, spec, result)
    from .worker_observation import observe_worker
    from .native_approval import approval_observation
    public_spec = spec.model_dump(mode="json")
    public_spec['provenance'].pop('native_approval', None)
    cancellation = {}
    intent_path = manager.registry.job_dir(run_id) / 'scheduler-cancellation.json'
    if (spec.route == 'ligand-first' and spec.provenance.get('worker_backend') == 'external-hpc'
            and (intent_path.exists() or intent_path.is_symlink())):
        from .scheduler_cancellation import read_intent
        intent = read_intent(manager.registry, run_id)
        confirmation_path = manager.registry.job_dir(run_id) / 'scheduler-cancellation-confirmed.json'
        confirmed = False
        if confirmation_path.is_symlink():
            raise ValueError('Symlinked scheduler confirmation')
        if confirmation_path.exists():
            from .native_approval import decode
            confirmation = decode(json.loads(confirmation_path.read_text())['proof'])
            confirmed = (confirmation.get('scope') == intent['scope']
                         and confirmation.get('cancellation_confirmed') is True
                         and result.state == 'CANCELLED'
                         and result.finished_at == confirmation.get('confirmed_at'))
        cancellation = {'cancellation_requested': True,
                        'cancellation_confirmed': confirmed,
                        'scheduler_cancellation': {'protocol': intent['protocol'], 'scope': intent['scope'],
                                                  'state': 'requested'}}
    return {
        "status": "PASS",
        "spec": public_spec,
        "approval_observation": approval_observation(spec),
        "job": result.model_dump(mode="json"),
        "worker_observation": observe_worker(manager.registry, spec, result),
        **cancellation,
    }


def _verify_observed_job_identity(run_id, spec, result):
    if (spec.run_id != run_id or result.run_id != run_id
            or result.engine != spec.engine or result.project_id != spec.project_id
            or result.route != spec.route):
        raise ValueError("Job result identity does not match its registered spec and requested handle")


def wait_job_tool(root: Path, run_id: str, *, timeout_seconds: int = 20) -> dict[str, Any]:
    """Bounded observation only: never submit, retry, cancel or infer success."""
    import time

    if type(timeout_seconds) is not int or not 0 <= timeout_seconds <= 30:
        raise ValueError("timeout_seconds must be an integer between 0 and 30")
    manager = JobManager(root)
    spec = manager.registry.load_spec(run_id)
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = manager.registry.load_result(run_id)
        _verify_observed_job_identity(run_id, spec, result)
        remaining = deadline - time.monotonic()
        if result.state not in {"QUEUED", "RUNNING"} or remaining <= 0:
            break
        time.sleep(min(0.25, remaining))
    terminal = result.state in {"COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"}
    from .worker_observation import observe_worker
    worker = observe_worker(manager.registry, spec, result)
    from .native_approval import approval_observation
    return {
        "status": "PASS",
        "run_id": run_id,
        "job": result.model_dump(mode="json"),
        "terminal": terminal,
        "observation_timed_out": not terminal and result.state != "PLANNED",
        "execution_succeeded": result.state == "COMPLETED",
        "scientific_result_verified": False,
        "worker_observation": worker,
        "approval_observation": approval_observation(spec),
        "next_action": (
            "reconcile_original_job_do_not_resubmit" if worker['reconciliation_required'] else
            "verify_outputs_before_advancing" if result.state == "COMPLETED" else
            "report_failure_do_not_resubmit" if terminal else
            "request_native_approval_before_execution" if result.state == "PLANNED" else
            "wait_again_on_same_run_id"
        ),
        "calculation_submitted": False,
    }


def get_job_log_tool(
    root: Path,
    run_id: str,
    *,
    stream: str = "stdout",
    max_lines: int = 200,
) -> dict[str, Any]:
    if stream not in {"stdout", "stderr"}:
        raise ValueError("stream must be stdout or stderr")
    log = JobManager(root).registry.tail_log(run_id, stream, max_lines=max_lines)
    return {"status": "PASS", "run_id": run_id, "stream": stream, "log": log}


def get_job_candidates_tool(root: Path, run_id: str, *, limit: int = 200) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError('limit must be an integer between 1 and 2000')
    manager = JobManager(root)
    result = manager.registry.load_result(run_id)
    spec = manager.registry.load_spec(run_id)
    verification = None
    scoring = {'kind': 'unclassified', 'activity_ranking_validated': False}
    if not result.candidate_table_path:
        return {"status": "PASS", "run_id": run_id, "count": 0, "columns": [], "candidates": [],
                "scientific_result_verified": False,
                "calculation_state": result.state, "calculation_completed": result.state == "COMPLETED",
                "failure_reason": result.failure_reason,
                "result_availability": "no_candidate_table_yet" if result.state in {"PLANNED", "QUEUED", "RUNNING"}
                    else "no_candidate_table_produced"}
    path = resolve_within(root, result.candidate_table_path)
    if spec.engine in {'covindb-prescreen', 'carsidock-cov', 'rtmscore'}:
        from .result_verification import verify_job
        verification = verify_job(root, run_id)
        if not verification['verified']:
            return {'status': verification['status'], 'run_id': run_id,
                    'verification': verification, 'scientific_result_verified': False,
                    'count': 0, 'candidates': [], 'result_availability': 'withheld_failed_or_incomplete_verification'}
        if not any(resolve_within(root, artifact.path) == path for artifact in result.artifacts):
            raise ValueError('Candidate table is not a verified registered artifact')
        stem = {'covindb-prescreen': 'stage2_candidates', 'carsidock-cov': 'carsidock_scores', 'rtmscore': 'rtmscore'}[spec.engine]
        output = (root / spec.output_directory).resolve()
        if path not in {output / (stem + '.csv'), output / (stem + '.parquet')}:
            raise ValueError('Candidate table differs from the engine output contract')
        if spec.engine == 'covindb-prescreen':
            manifest_path = (Path(spec.output_directory) / 'stage2_selection_manifest.json')
            if not manifest_path.is_absolute():
                manifest_path = root / manifest_path
            manifest = json.loads(manifest_path.read_text())
            contract = manifest.get('selection', {}).get('score_contract')
            scoring = {'kind': 'chemical_heuristic_prescreen',
                'algorithm_version': manifest.get('algorithm_version'),
                'contract_status': 'recorded' if contract else 'not_recorded_in_this_historical_output',
                'score_contract': contract, 'selection': manifest.get('selection'),
                'source_manifest': str(manifest_path.relative_to(root)),
                'source_manifest_sha256': sha256_file(manifest_path),
                'activity_ranking_validated': False, 'cross_channel_scores_comparable': False,
                'limitations': 'Chemical prioritization only, not docking, binding affinity or experimental activity. Never substitute the current scoring formula for an absent historical contract.'}
        else:
            scoring = {'kind': spec.engine, 'activity_ranking_validated': False,
                'score_direction': 'lower_is_better' if spec.engine == 'carsidock-cov' else 'higher_is_better',
                'limitations': 'Engine scores are method-specific computational estimates; they are not experimental activity, binding probability or directly comparable across engines. Failed rows are not successful scored candidates.'}
    if path.suffix.lower() == ".csv":
        import pandas as pd

        frame = pd.read_csv(path).head(limit)
        records = json.loads(frame.to_json(orient="records"))
        columns = list(frame.columns)
    elif path.suffix.lower() == ".parquet":
        import pandas as pd

        frame = pd.read_parquet(path).head(limit)
        records = json.loads(frame.to_json(orient="records"))
        columns = list(frame.columns)
    elif path.suffix.lower() == ".json":
        payload = json.loads(path.read_text())
        records = payload.get("candidates", [])[:limit] if isinstance(payload, dict) else payload[:limit]
        columns = sorted({key for row in records for key in row}) if records else []
    else:
        raise ValueError(f"unsupported candidate table type: {path.suffix}")
    gate_semantics = {
        "A": "physical PASS and AF3-CovDock consensus HIGH",
        "B1": "complete evidence with a soft physical warning or LOW consensus",
        "B2": "one or more required pose-quality checks are missing",
        "C": "one or more hard physical-validity checks failed",
    } if 'covdock_gate' in columns else {}
    if spec.engine == 'covindb-prescreen':
        gate_semantics = {
            'PASS': 'Eligible for chemical prescreen selection: compatible warhead, unambiguous electrophile and property window passed. Not docking or pose validation.',
            'REJECT': 'One or more chemical eligibility conditions failed; consult gate_reason and property_gate_reason.',
            'risk_annotations': 'pains_alert, high_reactivity_alert and aggregation_risk are separate alert fields, not the meaning of gate_status=PASS. Empty/false alerts mean not flagged by these rules, not experimentally safe.',
            'historical_formula': 'Only a recorded score_contract can establish the exact scoring formula and penalties for this output.',
        }
    report_lines = [f"任务：{run_id}；计算状态：{result.state}。", f"候选表（科学后端相对路径）：{path.relative_to(root)}"]
    if spec.engine == 'covindb-prescreen':
        report_lines.append('这是分子结构和理化性质预筛，不是对接或实验活性排名；不同筛选渠道分数不能直接混排。')
        report_lines.append('gate_status=PASS表示化学入选条件通过，不表示通过AF3/CovDock构象门控。PAINS、高反应性与聚集风险是独立警报字段；未触发警报不能写成实验安全，亦不能将其称为已通过的独立硬门。')
        if scoring.get('score_contract') is None:
            report_lines.append('本次历史产物未记录完整评分公式，不能用当前版本的公式补写解释。')
    elif spec.engine in {'carsidock-cov', 'rtmscore'}:
        report_lines.append(f"评分方向：{scoring['score_direction']}；仅适用于相同方法和输入条件，不代表实验活性。")
    for row in records:
        if row.get("covdock_gate") is None:
            continue
        detail = row.get("covdock_gate_detail") or row.get("covdock_gate")
        report_lines.extend([
            f"候选 {row.get('compound_id')}：Gate {detail}；physical_validity={row.get('physical_validity')}；cross_method_consensus={row.get('cross_method_consensus')}。",
            f"AF3–CovDock RMSD={row.get('af3_covdock_ligand_rmsd_angstrom')}；门控原因={row.get('covdock_gate_reason')}。",
            "A=物理检查通过且跨方法一致性高；B1=证据完整但有软警告或一致性低；B2=必需构象质量证据缺失；C=硬性物理检查失败。",
            "B2不等于全部门控通过。缺失RMSD仅说明未评估，不证明步骤被禁用；计算结果不证明实验活性或共价结合。",
        ])
    return {
        "status": "PASS",
        "run_id": run_id,
        "source": str(path.relative_to(root)),
        "calculation_state": result.state,
        "calculation_completed": result.state == "COMPLETED",
        "failure_reason": result.failure_reason,
        "result_availability": "completed_job_table" if result.state == "COMPLETED" else "partial_or_unvalidated_table",
        "scientific_result_verified": bool(verification and verification['verified']),
        "verification": verification,
        "scoring_interpretation": scoring,
        "gate_semantics": gate_semantics,
        "authoritative_report_markdown": "\n\n".join(report_lines),
        "count": len(records),
        "columns": columns,
        "candidates": records,
    }


def cancel_job_tool(root: Path, run_id: str, *, human_approved: bool = False) -> dict[str, Any]:
    if not human_approved:
        raise ValueError("cancelling a running job requires human_approved=true")
    result = JobManager(root).cancel(run_id)
    spec = ArtifactRegistry(root).load_spec(run_id)
    if spec.route == 'ligand-first' and spec.provenance.get('worker_backend') == 'external-hpc':
        from .scheduler_cancellation import read_intent
        intent = read_intent(ArtifactRegistry(root), run_id)
        return {'status': 'PASS', 'job': result.model_dump(mode='json'),
                'cancellation_requested': True, 'cancellation_confirmed': False,
                'scheduler_cancellation': {'protocol': intent['protocol'], 'scope': intent['scope'],
                                           'state': 'requested'},
                'next_action': 'The host must stop the exact reserved scheduler workload and verify its terminal state. '
                               'This signed intent does not prove scheduler or vendor-process termination.'}
    return {"status": "PASS", "job": result.model_dump(mode="json"),
            "cancellation_requested": True, "cancellation_confirmed": result.state == "CANCELLED",
            "next_action": "Observe this same run_id until terminal; request acknowledgement is not cancellation completion."}


def validate_project_tool(root: Path, project_id: str) -> dict[str, Any]:
    project_dir = resolve_within(root, Path("projects") / project_id)
    result = validate_project(project_dir)
    if result["status"] == "PASS":
        _, passport, assessment, route = load_project_state(root, project_id)
        result.update(
            {
                "target": passport.symbol,
                "site_id": passport.site.site_id,
                "decision": assessment.decision,
                "route": route.selected_route,
            }
        )
    return result


def plan_library_cascade_tool(
    root: Path,
    request_path: str,
    *,
    library_size: int | None = None,
    route_override_approved: bool = False,
    allow_proprietary: bool = False,
    require_inputs: bool = True,
    availability: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_abs = resolve_within(root, request_path)
    request = LibraryFirstRequest.model_validate(read_json(request_abs))
    project_dir, _, assessment, route = load_project_state(root, request.project_id)
    request_abs.relative_to(project_dir)
    validation = validate_library_first_request(request, project_dir, require_inputs=require_inputs)
    if validation["status"] != "PASS":
        return {"status": "FAIL", "validation": validation}
    if library_size is None:
        library_size = count_library_rows(project_dir / request.ligand_table_path)
    if library_size <= 0:
        raise ValueError("library_size must be positive")
    engine_availability = (
        EngineAvailability.model_validate(availability)
        if availability is not None
        else infer_engine_availability(root)
    )
    cascade_policy = CascadePolicy.model_validate(policy or {})
    cascade_policy = cascade_policy.model_copy(update={"allow_proprietary": allow_proprietary})
    plan = plan_library_first_cascade(
        request,
        assessment,
        route,
        library_size,
        engine_availability,
        cascade_policy,
        route_override_approved=route_override_approved,
    )
    return {
        "status": "PASS",
        "validation": validation,
        "availability": engine_availability.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
    }


def next_library_action_tool(
    plan: dict[str, Any],
    observation: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action = choose_next_action(
        AdaptiveCascadePlan.model_validate(plan),
        CascadeObservation.model_validate(observation),
        CascadePolicy.model_validate(policy or {}),
    )
    return {"status": "PASS", "next_action": action.model_dump(mode="json")}


def plan_attachments_tool(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    request = AttachmentRequest.model_validate(payload)
    _, passport, assessment, route = load_project_state(root, request.project_id)
    result = plan_attachments(request, passport, assessment, route)
    return {"status": "PASS", "attachment_plan": result.model_dump(mode="json")}


def enumerate_covalent_candidates_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Run only deterministic, local RDKit enumeration; no docking or model execution."""
    result = enumerate_covalent_candidates(CovalentEnumerationRequest.model_validate(payload))
    return {"status": result.status, "enumeration": result.model_dump(mode="json")}


def plan_ligand_first_tool(
    root: Path,
    request_path: str,
    *,
    require_inputs: bool = True,
) -> dict[str, Any]:
    request_abs = resolve_within(root, request_path)
    request = LigandFirstRequest.model_validate(read_json(request_abs))
    project_dir, passport, assessment, route = load_project_state(root, request.project_id)
    request_abs.relative_to(project_dir)
    validation_errors: list[str] = []
    generated_available = False
    screen_available = False
    for field, relative in (
        ("generated_candidates_path", request.generated_candidates_path),
        ("covalent_screen_results_path", request.covalent_screen_results_path),
    ):
        if relative is None:
            continue
        try:
            candidate = resolve_within(project_dir, relative)
        except ValueError:
            validation_errors.append(f"{field} escapes the project directory")
            continue
        available = candidate.is_file()
        if field == "generated_candidates_path":
            generated_available = available
        else:
            screen_available = available
        if require_inputs and not available:
            validation_errors.append(f"{field} does not exist: {relative}")
    if validation_errors:
        return {"status": "FAIL", "validation": {"status": "FAIL", "errors": validation_errors}}
    plan = plan_ligand_first(
        request,
        passport,
        assessment,
        route,
        generated_candidates_available=generated_available,
        covalent_screen_results_available=screen_available,
    )
    return {
        "status": "PASS",
        "validation": {"status": "PASS", "errors": []},
        "plan": plan.model_dump(mode="json"),
    }


def evaluate_ligand_first_candidate_tool(
    root: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Independently recompute Ligand-first chemistry and geometry gates without writing files."""
    required = (
        "project_id",
        "campaign_path",
        "candidate_json_path",
        "candidate_sdf_path",
        "geometry_json_path",
        "raw_poses_path",
        "no_dummy_poses_path",
        "expected_carsidock_run_id",
    )
    missing = [name for name in required if not payload.get(name)]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")

    project_dir, _, _, _ = load_project_state(root, str(payload["project_id"]))

    def project_file(field: str) -> Path:
        path = resolve_within(project_dir, str(payload[field]))
        if not path.is_file():
            raise FileNotFoundError(f"{field} does not exist: {payload[field]}")
        return path

    campaign_path = project_file("campaign_path")
    candidate_json_path = project_file("candidate_json_path")
    candidate_sdf_path = project_file("candidate_sdf_path")
    geometry_json_path = project_file("geometry_json_path")
    raw_poses_path = project_file("raw_poses_path")
    no_dummy_poses_path = project_file("no_dummy_poses_path")

    campaign = load_campaign(campaign_path)
    campaign_dir = campaign_path.parent
    parent_pose = build_parent_pose_mol(campaign, campaign_dir)

    candidate_payload = read_json(candidate_json_path)
    candidates = candidate_payload.get("candidates", [])
    if len(candidates) != 1:
        raise ValueError(
            "independent evaluator v1 requires exactly one candidate in candidate_json_path"
        )
    candidate_molecules = [
        molecule
        for molecule in Chem.SDMolSupplier(
            str(candidate_sdf_path), removeHs=False, sanitize=True
        )
        if molecule is not None
    ]
    if len(candidate_molecules) != 1:
        raise ValueError(
            "independent evaluator v1 requires exactly one valid molecule in candidate_sdf_path"
        )

    saved_geometry = LigandFirstGeometryResult.model_validate_json(
        geometry_json_path.read_text()
    )
    receptor_pdb = resolve_within(campaign_dir, campaign.parent_pose.structure_path)
    if not receptor_pdb.is_file():
        raise FileNotFoundError(
            f"campaign receptor structure does not exist: {campaign.parent_pose.structure_path}"
        )
    evaluation = independently_evaluate_candidate(
        campaign=campaign,
        campaign_dir=campaign_dir,
        parent_pose=parent_pose,
        candidate_record=candidates[0],
        candidate_mol=candidate_molecules[0],
        saved_geometry=saved_geometry,
        expected_carsidock_run_id=str(payload["expected_carsidock_run_id"]),
        raw_poses=load_sdf_poses(raw_poses_path),
        no_dummy_poses=load_sdf_poses(no_dummy_poses_path),
        receptor_pdb=receptor_pdb,
    )
    return {
        "status": "PASS",
        "candidate_id": candidates[0]["candidate_id"],
        "independent_evaluation": evaluation.model_dump(mode="json"),
    }


def plan_de_novo_tool(
    root: Path, request_path: str, *, require_inputs: bool = True,
    availability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compatibility entry; maintain the implementation in the De novo route."""
    from .layout import enable_de_novo
    enable_de_novo()
    from covscreen_de_novo.planning import plan_request
    return plan_request(root, request_path, require_inputs=require_inputs, availability=availability)


def capabilities_tool(root: Path) -> dict[str, Any]:
    # Import at call time: route adapters themselves import this module.
    # Discovery and documentation must use the same definitions.
    from .mcp.registry import SERVER_VERSION, TOOLS, groups
    from .metric_contracts import metric_contracts

    return {
        "status": "PASS",
        "server": "covalent-discovery-tools",
        "server_version": SERVER_VERSION,
        "tools": [tool["name"] for tool in TOOLS],
        "tool_count": len(TOOLS),
        "approval_service_availability": "NOT_VERIFIED_BY_CAPABILITY_DISCOVERY",
        "tool_groups": groups(),
        "metric_contracts": metric_contracts(),
        "library_first": {
            "receptor_repair": _receptor_repair_capability(),
            "real_new_candidate_end_to_end_validation_complete": False,
        },
        "execution_boundary": (
            "MCP execution calls require the host's signed native approval receipt bound to the exact "
            "execution scope and resource budget. human_approved=true and an approval note alone do "
            "not authorize execution. Arbitrary commands are never accepted. Tool groups describe "
            "maintenance ownership, not route visibility or runtime/scientific readiness."
        ),
        "project_root": str(root.resolve()),
    }
