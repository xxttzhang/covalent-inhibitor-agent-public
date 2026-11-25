"""Replay the existing receptor audits for a bound screening handoff.

This verifies molecular evidence, not native permission or biological fitness.
The caller owns workflow/job approval; no task is submitted here. Recorded
reports alone never stand in for the actual identity/geometry implementations.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile

from .jobs import ArtifactRegistry, sha256_file
from .receptor_input import load_receptor_preparation_input
from .receptor_admission import admit_isolated_receptor
from .receptor_quality import audit_relaxed_receptor
from .receptor_geometry import audit_receptor_geometry
from .receptor_site_context import assess_receptor_site_context, SITE_CONTEXT_POLICY_V1


def regular(root, path, expected=None):
    root, path = Path(root).resolve(), Path(path)
    if not path.is_absolute():
        path = root / path
    relative = path.relative_to(root)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Receptor evidence path is not canonical")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Receptor evidence cannot traverse symbolic links")
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Receptor evidence is not a bounded regular file")
    digest = sha256_file(path)
    if expected is not None and digest != expected:
        raise ValueError("Receptor evidence checksum mismatch: " + relative.as_posix())
    return path, digest


def sources(source_kind=None):
    from . import (receptor_admission, receptor_quality, receptor_geometry, receptor_site_context,
                   peptide_templates, receptor_preparation, receptor_input, pdb_coordinates,
                   target_acquisition_artifact, target_identity)
    modules = (receptor_admission, receptor_quality, receptor_geometry, receptor_site_context, peptide_templates,
               receptor_preparation, receptor_input, pdb_coordinates, target_acquisition_artifact, target_identity)
    if source_kind in {'sealed-ligand-study-v1', 'sealed-ligand-vs-receptor-v1'}:
        from . import ligand_receptor_input, ligand_refinement_target, ligand_refinement_input, ligand_design
        modules += (ligand_receptor_input, ligand_refinement_target, ligand_refinement_input, ligand_design)
    if source_kind == 'sealed-ligand-vs-receptor-v1':
        from . import ligand_vs_input, ligand_receptor_source
        modules += (ligand_vs_input, ligand_receptor_source)
    return {module.__name__: sha256_file(Path(module.__file__)) for module in modules} | {
                __name__: sha256_file(Path(__file__))}


def inputs(root, *, preparation_run_id, receptor_input_artifact_id, acquisition_sha256,
           prepared_sha256, context_artifact_id=None, context_sha256=None):
    """Read a completed preparation; optional context is supplied after measurement.

    Omitting context only validates preparation inputs, never completes replay.
    """
    root = Path(root).resolve()
    registry = ArtifactRegistry(root, create=False)
    job = registry.job_dir(preparation_run_id)
    inventory = {}
    def file(path, expected=None):
        path, digest = regular(root, path, expected)
        inventory[path.relative_to(root).as_posix()] = digest
        return path
    def read(path, expected=None):
        return json.loads(file(path, expected).read_text())
    file(job / "spec.json")
    file(job / "result.json")
    spec, result = registry.load_spec(preparation_run_id), registry.load_result(preparation_run_id)
    if (spec.engine != "library-first-receptor-preparation" or result.state != "COMPLETED"
        or result.return_code != 0 or spec.execution_mode != "execute"
        or any(getattr(spec, name) != getattr(result, name) for name in
               ("run_id", "project_id", "engine", "route", "attempt", "parent_run_id"))
        or spec.parameters.get("receptor_input_artifact_id") != receptor_input_artifact_id
        or spec.parameters.get("manifest_sha256") != acquisition_sha256):
        raise ValueError("Receptor evidence requires its exact completed preparation job")
    for name, digest in spec.input_sha256.items():
        file(name, digest)
    # Validate paths before the existing loader dereferences them.
    if not re.fullmatch(r"receptor-input-[a-f0-9]{32}", receptor_input_artifact_id):
        raise ValueError("Invalid receptor input artifact ID")
    acquisition = root / "state/target_acquisition" / receptor_input_artifact_id[15:]
    read(acquisition / "target_acquisition.json", acquisition_sha256)
    loaded = load_receptor_preparation_input(root, receptor_input_artifact_id, acquisition_sha256)
    if (spec.route != loaded['source_route']
            or loaded.get('source_project_id', spec.project_id) != spec.project_id):
        raise ValueError('Receptor evidence belongs to another route or study')
    target_id = loaded['target_artifact_id']
    for path in loaded['evidence_files']:
        file(path)
    pipeline = job / "artifacts/receptor-preparation"
    manifest = read(pipeline / "pipeline.json")
    if (manifest.get("schema") != "receptor-preparation-pipeline-v1"
        or manifest.get("status") != "prepared_isolated_protein_scope"
        or manifest.get("target_acquisition_sha256") != acquisition_sha256
        or manifest.get("target_artifact_id") != target_id
        or manifest.get("prepared_manifest_sha256") != prepared_sha256
        or any(manifest.get(key) != spec.parameters.get(key) for key in ("altlocs", "heavy_seed", "max_missing_residues"))):
        raise ValueError("Receptor pipeline differs from its registered inputs")
    if (loaded['source_kind'] in {'sealed-ligand-study-v1', 'sealed-ligand-vs-receptor-v1'}
            or 'input_source_kind' in manifest or 'input_source_route' in manifest):
        if (manifest.get('input_source_kind') != loaded['source_kind']
                or manifest.get('input_source_route') != loaded['source_route']):
            raise ValueError('Receptor pipeline source differs from its registered study')
    artifacts = manifest.get("artifacts", {})
    required = {"heavy/receipt.json", "heavy/inventory.json", "relaxation/receipt.json",
        "relaxation/relaxed.pdb", "identity-quality.json", "geometry-quality.json", "prepared/prepared-receptor.json"}
    if not required.issubset(artifacts) or len(artifacts) > 100:
        raise ValueError("Receptor pipeline evidence inventory is incomplete")
    for name, digest in artifacts.items():
        if not isinstance(name, str) or name.startswith("/") or ".." in Path(name).parts:
            raise ValueError("Receptor pipeline artifact escaped its output")
        file(pipeline / name, digest)
    prepared = read(pipeline / "prepared/prepared-receptor.json", prepared_sha256)
    for name, digest in prepared.get('artifacts', {}).items():
        if artifacts.get('prepared/' + name) != digest:
            raise ValueError('Prepared receptor file is absent from the pipeline inventory')
        file(pipeline / 'prepared' / name, digest)
    evidence = {"loaded": loaded, "pipeline": pipeline, "prepared": prepared,
                "inventory": dict(sorted(inventory.items())), "sources": sources(loaded['source_kind'])}
    if context_artifact_id is None and context_sha256 is None:
        return evidence
    if not isinstance(context_artifact_id, str) or not isinstance(context_sha256, str):
        raise ValueError('Receptor context requires both identity and checksum')
    if not re.fullmatch(r"receptor-context-[a-f0-9]{32}", context_artifact_id):
        raise ValueError("Invalid receptor context ID")
    context_path = root / "state/receptor_site_context" / context_artifact_id[17:] / "site-context.json"
    context = read(context_path, context_sha256)
    expected = {"target_acquisition_sha256": acquisition_sha256, "prepared_receptor_sha256": prepared_sha256}
    if (context.get("schema") != "receptor-site-context-artifact-v1"
        or context.get("status") != "descriptive_site_context_completed_review_pending"
        or context.get("parents") != expected or context.get("policy") != SITE_CONTEXT_POLICY_V1):
        raise ValueError("Receptor context differs from the bound preparation")
    return {**evidence, "context": context, "inventory": dict(sorted(inventory.items()))}


def semantic_audit(value):
    # Package/compiler identities are reported separately. They do not replace
    # replayed scientific checks; every other field must reproduce exactly.
    return {key: item for key, item in value.items()
            if key not in {"rdkit_version", "numpy_version", "audit_source_sha256", "template_source_sha256"}}


def replay(root, evidence):
    pipeline, loaded = evidence["pipeline"], evidence["loaded"]
    source = loaded["acquisition_directory"] / "structure.pdb"
    relaxation = pipeline / "relaxation"
    relaxed_sha = sha256_file(relaxation / "receipt.json")
    quality_path, geometry_path = pipeline / "identity-quality.json", pipeline / "geometry-quality.json"
    quality = audit_relaxed_receptor(relaxation, relaxed_sha, pipeline / "heavy", source)
    if quality["status"] != "identity_and_stereochemistry_passed_geometry_pending":
        raise ValueError("Replayed receptor identity/stereochemistry did not pass")
    if semantic_audit(quality) != semantic_audit(json.loads(quality_path.read_text())):
        raise ValueError("Recorded receptor identity audit differs from recomputation")
    geometry = audit_receptor_geometry(relaxation, relaxed_sha, quality_path, sha256_file(quality_path), source)
    if geometry["status"] != "geometry_passed_environment_review_pending":
        raise ValueError("Replayed receptor geometry did not pass")
    if semantic_audit(geometry) != semantic_audit(json.loads(geometry_path.read_text())):
        raise ValueError("Recorded receptor geometry audit differs from recomputation")
    with tempfile.TemporaryDirectory(prefix="receptor-handoff-audit-") as temporary:
        prepared = Path(temporary) / "prepared"
        acquisition_sha = evidence["prepared"]["parents"]["target_acquisition_sha256"]
        admitted = admit_isolated_receptor(loaded["acquisition_directory"], acquisition_sha,
            loaded["canonical_sequence"], loaded["taxon"], pipeline / "heavy",
            relaxation, relaxed_sha, quality_path, sha256_file(quality_path), geometry_path, sha256_file(geometry_path), prepared)
        if admitted != evidence["prepared"]:
            raise ValueError("Prepared receptor artifact differs from replayed admission")
        context = assess_receptor_site_context(loaded["acquisition_directory"], acquisition_sha,
            loaded["acquisition"], pipeline / "prepared", sha256_file(pipeline / "prepared/prepared-receptor.json"),
            Path(temporary) / "context")
    stored, actual = deepcopy(evidence["context"]), deepcopy(context)
    stored["prepared_isolated_protein"].pop("runtime", None)
    actual["prepared_isolated_protein"].pop("runtime", None)
    if stored != actual:
        raise ValueError("Stored site context differs from recomputed measurements")
    # Detect source replacement during an expensive audit before committing it.
    for name, digest in evidence["inventory"].items():
        regular(root, name, digest)
    if evidence["sources"] != sources(loaded['source_kind']):
        raise ValueError("Receptor audit implementation changed during replay")
    return {"status": "RECEPTOR_EVIDENCE_REPLAYED", "identity": context["identity"],
        "scope": "isolated_selected_chain_protein", "quality": quality, "geometry": geometry,
        "context": context, "sources": evidence["sources"], "inventory": evidence["inventory"],
        "receptor": {"path": str((pipeline / "prepared/prepared-receptor.pdb").relative_to(Path(root).resolve())),
                     "sha256": evidence["prepared"]["artifacts"]["prepared-receptor.pdb"]},
        "compute_submitted": False, "screening_authorized": False, "campaign_ready": False,
        "limitations": ["Structural checks do not establish cysteine reactivity or ligand activity.",
            "Assembly, removed-component relevance and reaction scope require scientific review.",
            "A separate exact plan and native approval are required before screening."]}
