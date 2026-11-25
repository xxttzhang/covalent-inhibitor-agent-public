"""Verify a retained candidate against immutable inputs of an interrupted run.

This descriptor authorizes no computation and does not mark its parent complete.
It binds a readback audit and every retained pose to the original candidate.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pandas as pd

from .jobs import sha256_file, ArtifactRegistry, JobSubmissionRequest, ResourceBudget

RETAINED_POLICY = 'ligand-hpc-retained-v1'


def retained_source_parent(root: Path, parent_run_id: str) -> str:
    """Allow one project-storage retry only after a pre-docking EDQUOT failure."""
    registry = ArtifactRegistry(root, create=False)
    parent = registry.load_spec(parent_run_id)
    if parent.attempt != 3:
        return parent_run_id
    result = registry.load_result(parent_run_id)
    stderr = registry.job_dir(parent_run_id) / 'stderr.log'
    text = stderr.read_text() if stderr.is_file() else ''
    artifacts = registry.job_dir(parent_run_id) / 'artifacts'
    if (parent.parameters.get('dispatch_policy') != RETAINED_POLICY
            or parent.engine != 'schrodinger-covdock-lo' or parent.route != 'ligand-first'
            or result.state != 'FAILED' or result.failure_reason != 'engine_return_code_1'
            or '[Errno 122]' not in text or 'Disk quota exceeded' not in text
            or 'subset.to_parquet(group_table, index=False)' not in text
            or result.candidate_table_path is not None
            or Path(parent.working_directory).resolve() == root.resolve()
            or not parent.parent_run_id
            or any(artifacts.rglob('*-out.maegz'))
            or any(artifacts.rglob('shard_*'))):
        raise ValueError('Storage recovery requires a relocated, pre-docking EDQUOT failure')
    validate_retained_spec(root, parent)
    return parent.parent_run_id


def compile_retained_request(root: Path, parent_run_id: str, records: list, *,
                             execution_mode='plan', human_approved=False,
                             approval_note=None) -> JobSubmissionRequest:
    """Prepare the exact 3-candidate recovery; never register or execute a job."""
    registry = ArtifactRegistry(root)
    parent = registry.load_spec(parent_run_id)
    if parent.attempt == 3:
        source = retained_source_parent(root, parent_run_id)
        expected = compile_retained_request(root, source, records,
            execution_mode=execution_mode, human_approved=human_approved,
            approval_note=approval_note)
        if parent.parameters != expected.parameters or parent.resource_budget != expected.resource_budget:
            raise ValueError('Storage recovery must preserve every scientific parameter and budget')
        return expected.model_copy(update={'parent_run_id': parent_run_id, 'attempt': 4,
            'request_id': parent.request_id + '-retry-4'})
    result = registry.load_result(parent_run_id)
    if (parent.engine != 'schrodinger-covdock-lo' or parent.route != 'ligand-first'
            or parent.attempt != 2 or parent.provenance.get('worker_backend') != 'external-inspire'
            or parent.parameters.get('dispatch_policy') != 'ligand-smoke-v1'
            or parent.parameters.get('input_row_count') != 3
            or parent.parameters.get('njobs') != 1
            or result.state != 'TIMED_OUT'
            or result.failure_reason != 'external_scheduler_timed_out'):
        raise ValueError('Retained recovery requires the exact timed-out serial attempt')
    parameters = dict(parent.parameters)
    parameters.update(dispatch_policy=RETAINED_POLICY, checkpoint_size=1,
        checkpoint_workers=2, checkpoint_worker_cpus=4, njobs=1,
        host='localhost:1', resume=False)
    retained = validate_retained_recovery(root, parameters, records, parent_run_id)
    if len(retained) != 1 or any(v['original_candidate_count'] != 3 for v in retained.values()):
        raise ValueError('Retained recovery must preserve one candidate and compute the other two')
    parameters['retained_candidates'] = [
        {'path': str(v['descriptor_path'].relative_to(root.resolve())),
         'sha256': v['descriptor_sha256']} for v in retained.values()]
    # The current compiler freezes this derived receipt. Reconstruct it from
    # the hash-verified receptor so ancestry comparison uses the same contract,
    # including when the historical parent predates receptor inspection.
    from .covdock_receptor import inspect_receptor
    parameters['covdock_receptor_input_check'] = inspect_receptor(
        root / parameters['receptor'], parameters['attachment_residue'],
        parameters.get('nucleophilic_atom', 'SG'))
    return JobSubmissionRequest(request_id=parent.request_id + '-retry-3',
        project_id=parent.project_id, route=parent.route, engine=parent.engine,
        parent_run_id=parent.run_id, attempt=3, parameters=parameters,
        execution_mode=execution_mode, human_approved=human_approved,
        approval_note=approval_note,
        resource_budget=ResourceBudget(max_runtime_minutes=180, cpu_count=8,
            memory_gb=32, gpu_count=0, max_candidates=3))


def validate_retained_spec(root: Path, spec) -> None:
    """Check ancestry and frozen dependencies before approval or dispatch."""
    expected = compile_retained_request(root, spec.parent_run_id,
        spec.parameters.get('retained_candidates'), execution_mode=spec.execution_mode,
        human_approved=spec.human_approved, approval_note=spec.approval_note)
    for name in ('request_id', 'project_id', 'route', 'engine', 'attempt',
                 'parent_run_id', 'parameters', 'resource_budget'):
        if getattr(spec, name) != getattr(expected, name):
            raise ValueError('Retained recovery changes the bounded request: ' + name)
    verified = validate_retained_recovery(root, spec.parameters,
        spec.parameters['retained_candidates'], retained_source_parent(root, spec.parent_run_id))
    if spec.attempt == 4:
        for name in ('spec.json', 'result.json', 'stderr.log'):
            path = root / 'state/jobs' / spec.parent_run_id / name
            if spec.input_sha256.get(str(path.relative_to(root))) != sha256_file(path):
                raise ValueError('Storage failure evidence is not frozen: ' + name)
    for candidate in verified.values():
        for path in [candidate['descriptor_path'], *candidate['files'].values()]:
            relative = str(path.relative_to(root.resolve()))
            if spec.input_sha256.get(relative) != sha256_file(path):
                raise ValueError('Retained dependency is not frozen in job inputs: ' + relative)


# Reparse must use the same chemistry and physical gates as new candidate jobs.
SCIENTIFIC_DEFAULTS = {
    'attachment_residue': None, 'nucleophilic_atom': 'SG',
    'reaction_type_column': 'covdock_reaction_type', 'prepare_receptor': False,
    'min_radius': None, 'score_property': None,
    'max_local_strain': 10.0, 'hard_max_strain': 20.0,
    'min_covalent_bond_length': 1.5, 'max_covalent_bond_length': 2.2,
    'max_pose_rmsd': 3.0, 'severe_clash_distance': 1.2, 'min_success_rate': 0.80,
}


def validate_retained_recovery(root: Path, parameters: dict, records: list,
                               parent_run_id: str | None = None) -> dict[int, dict]:
    """Bind every reused candidate to the full input and unchanged scientific rules."""
    root = root.resolve()
    if not isinstance(records, list) or not records:
        raise ValueError('Retained candidate descriptors must be a nonempty list')
    if (parameters.get('checkpoint_size') != 1 or parameters.get('reuse_artifact_dir')
            or parameters.get('pose_consistency_table')):
        raise ValueError('Retained recovery requires single-candidate shards without other reuse or consensus input')
    retained = {}
    parents = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {'path', 'sha256'}:
            raise ValueError('Retained descriptor needs exactly path and sha256')
        path = (root / record['path']).resolve()
        path.relative_to(root)
        if sha256_file(path) != record['sha256']:
            raise ValueError('Retained descriptor changed')
        verified = validate_retained_candidate(root, path)
        parent = json.loads(verified['files']['parent_spec'].read_text())
        parents.add(parent['run_id'])
        if parent_run_id is not None and parent['run_id'] != parent_run_id:
            raise ValueError('Retained descriptor belongs to another retry parent')
        for name in ('candidate_table', 'receptor'):
            if (root / parameters[name]).resolve() != verified['files'][name]:
                raise ValueError('Recovery changes original scientific input: ' + name)
        for name, default in SCIENTIFIC_DEFAULTS.items():
            if parameters.get(name, default) != parent['parameters'].get(name, default):
                raise ValueError('Recovery changes original chemistry or gate: ' + name)
        for name in ('grid_file', 'pose_consistency_table'):
            if parameters.get(name) or parent['parameters'].get(name):
                raise ValueError('Retained recovery does not yet support: ' + name)
        if parameters.get('score_property') not in (None, 'r_cdock_cdock_affinity'):
            raise ValueError('Retained recovery requires the original affinity selection')
        index = int(verified['descriptor']['input_sdf_index'])
        if index in retained:
            raise ValueError('Duplicate retained candidate identity')
        verified['descriptor_path'] = path
        retained[index] = verified
    if len(parents) != 1:
        raise ValueError('Retained candidates must share one original parent')
    return retained


def selection_inspection(inspection: dict) -> tuple[dict, list[int]]:
    """Retain every raw row while masking the vendor's documented failure score."""
    result = deepcopy(inspection)
    invalid = []
    for row in result['rows']:
        if row.get('r_i_score_docking_score') == 999:
            row['raw_r_cdock_cdock_affinity'] = row.get('r_cdock_cdock_affinity')
            row['r_cdock_cdock_affinity'] = None
            row['score_validity_reason'] = 'vendor_score_failure_sentinel_999'
            invalid.append(row['pose_index'])
    return result, invalid


def validate_retained_candidate(root: Path, descriptor: Path) -> dict:
    root = root.resolve()
    descriptor = descriptor.resolve()
    descriptor.relative_to(root)
    data = json.loads(descriptor.read_text())
    if data.get("kind") != "covdock-retained-candidate-v1":
        raise ValueError("Unsupported retained candidate descriptor")
    files = {}
    for key in ("parent_spec", "parent_result", "candidate_table", "receptor", "pose_file", "readback_audit"):
        record = data["files"][key]
        path = (root / record["path"]).resolve()
        path.relative_to(root)
        if not path.is_file() or sha256_file(path) != record["sha256"]:
            raise ValueError("Retained source changed: " + key)
        files[key] = path
    parent = root / "state/jobs" / data["parent_run_id"]
    if files["parent_spec"] != parent / "spec.json" or files["parent_result"] != parent / "result.json":
        raise ValueError("Retained parent identity mismatch")
    spec = json.loads(files["parent_spec"].read_text())
    result = json.loads(files["parent_result"].read_text())
    if (spec["run_id"] != data["parent_run_id"] or result["run_id"] != spec["run_id"]
            or spec["engine"] != "schrodinger-covdock-lo" or spec["route"] != "ligand-first"
            or result["state"] != "TIMED_OUT"):
        raise ValueError("Expected an interrupted Ligand-first CovDock parent")
    for key in ("candidate_table", "receptor"):
        path = (root / spec["parameters"][key]).resolve()
        if files[key] != path or spec["input_sha256"].get(str(path.relative_to(root))) != sha256_file(path):
            raise ValueError("Retained scientific input differs from parent: " + key)
    files["pose_file"].relative_to(parent / "artifacts")
    frame = pd.read_parquet(files["candidate_table"])
    if frame["input_sdf_index"].duplicated().any() or frame["compound_id"].duplicated().any():
        raise ValueError("Original candidate identity is ambiguous")
    selected = frame.loc[frame["compound_id"].eq(data["compound_id"])]
    if len(selected) != 1 or int(selected.iloc[0]["input_sdf_index"]) != data["input_sdf_index"]:
        raise ValueError("Retained candidate absent from original denominator")
    audit = json.loads(files["readback_audit"].read_text())
    # Historical audit paths remain immutable after a byte-identical storage
    # migration. Resolve their original location from the hash-bound parent.
    audit_source = str(files['pose_file'])
    if audit_source not in audit.get('sources', {}):
        historical = Path(spec['working_directory']) / data['files']['pose_file']['path']
        if not historical.is_absolute() or not historical.is_file() or sha256_file(historical) != sha256_file(files['pose_file']):
            raise ValueError('Relocated pose does not match its historical audited source')
        audit_source = str(historical)
    if (audit.get("kind") != "READ_ONLY_RETAINED_CANDIDATE_AUDIT"
            or audit.get("parent_run_state") != "TIMED_OUT"
            or audit.get("new_docking_or_scoring_run") is not False
            or audit.get("retained_066_identity_hash_verified") is not True
            or audit.get("sources", {}).get(audit_source) != sha256_file(files["pose_file"])):
        raise ValueError("Retained readback does not bind the actual pose source")
    rows = audit["rows"]
    if not rows or len(rows) != data["pose_count"] or len(rows) != audit["retained_pose_count"]:
        raise ValueError("Retained pose denominator mismatch")
    if [row["pose_index"] for row in rows] != list(range(len(rows))):
        raise ValueError("Retained pose indices must be complete and ordered")
    title = data["title"]
    compound = re.sub(r"[^A-Za-z0-9._-]+", "_", data["compound_id"]).strip("_")[:60] or "compound"
    if not title.startswith(compound + "__p") or not title.endswith("__i" + str(data["input_sdf_index"])):
        raise ValueError("Retained title differs from the requested original candidate")
    prepared = [row for row in audit["prepared_inputs"] if row["title"] == title]
    if len(prepared) != 1 or prepared[0]["vendor_input_hash"] != data["vendor_input_hash"]:
        raise ValueError("Retained prepared-input hash mismatch")
    for row in rows:
        # Output titles may include the receptor prefix; require the exact
        # original ligand title at the end, rather than a substring identity.
        if (not row["title"].endswith(title)
                or row["vendor_input_hash"] != data["vendor_input_hash"]
                or row["matching_input_indices"] != [prepared[0]["index"]]):
            raise ValueError("Retained pose belongs to another candidate")
    return {"descriptor": data, "files": files, "candidate": selected.copy(),
            "original_candidate_count": len(frame), "audit": audit,
            "descriptor_sha256": sha256_file(descriptor)}
