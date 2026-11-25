"""Verify registered preparation receipts; this does not rerun scientific engines."""
import json
import math

from covscreen.jobs import sha256_file


def registered_output(root, folder, spec, result, name, digest=None):
    path = folder / name
    if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
        raise ValueError("MD artifact is missing or is a symbolic link: " + name)
    item = next((item for item in result.artifacts if item.path == str(path.relative_to(root))), None)
    actual = sha256_file(path)
    if (not item or item.run_id != spec.run_id or item.sha256 != actual
            or item.size_bytes != path.stat().st_size or digest is not None and digest != actual):
        raise ValueError("MD artifact differs from registered evidence: " + name)
    return path


def preparation_receipt(root, record, manager, spec, result, parent):
    if result.state != "COMPLETED" or result.return_code != 0:
        raise ValueError("System preparation has not completed successfully")
    folder = manager.registry.job_dir(spec.run_id) / "artifacts/openmm-system-preparation"
    def file(name, digest=None):
        return registered_output(root, folder, spec, result, name, digest)

    receipt = file("openmm-preparation.json")
    checksum = sha256_file(receipt)
    prior = record.get("system_preparation")
    if prior and prior["manifest_sha256"] != checksum:
        raise ValueError("Previously verified system preparation changed")
    value = json.loads(receipt.read_text())
    stages = value.get("stages", {})
    conversion_path = file("conversion/conversion.json", stages.get("conversion_manifest_sha256"))
    forcefield_path = file("forcefield-audit/forcefield-audit.json", stages.get("forcefield_audit_manifest_sha256"))
    minimization_path = file("minimization/minimization.json", stages.get("minimization_manifest_sha256"))
    expected_stages = {"conversion_manifest_sha256": sha256_file(conversion_path),
        "forcefield_audit_manifest_sha256": sha256_file(forcefield_path),
        "minimization_manifest_sha256": sha256_file(minimization_path)}
    conversion, forcefield, minimization = [json.loads(path.read_text())
        for path in (conversion_path, forcefield_path, minimization_path)]
    audit_path = manager.registry.job_dir(parent["run_id"]) / "artifacts/adduct-parameterization/parameterization-audit.json"
    audit = json.loads(audit_path.read_text())  # Caller freshly validates the signed parameterization and its bytes.
    parents = {"source_cms_sha256": parent["parameterized_cms_sha256"], "parameter_audit_sha256": parent["manifest_sha256"]}
    if (value.get("schema") != "openmm-system-preparation-artifact-v1"
            or value.get("status") != "OPENMM_SYSTEM_MINIMIZED_SHORT_QC_PENDING"
            or value.get("parents") != parents or stages != expected_stages
            or value.get("independent_forcefield_audit_completed") is not True
            or value.get("bounded_minimization_completed") is not True
            or any(document.get("system_id") != parent["candidate_id"] or document.get("atoms") != audit["total_atoms"]
                   for document in (value, conversion, forcefield, minimization))
            or any(document.get(key) is not False for document in (value, conversion, forcefield, minimization)
                   for key in ("simulation_started", "md_system_ready", "campaign_ready"))
            or any(document.get(key) is not False for document in (value, minimization)
                   for key in ("short_qc_completed", "equilibration_completed"))):
        raise ValueError("Preparation receipt does not bind the completed stages and parameterization")
    if (conversion.get("schema") != "openmm-conversion-artifact-v1"
            or conversion.get("status") != "conversion_written_forcefield_audit_pending"
            or forcefield.get("schema") != "openmm-forcefield-audit-artifact-v1"
            or forcefield.get("status") != "OPENMM_FORCEFIELD_AUDITED_MINIMIZATION_PENDING"
            or forcefield.get("independent_forcefield_audit_completed") is not True
            or forcefield.get("conversion_manifest_sha256") != expected_stages["conversion_manifest_sha256"]
            or any(document.get(key) != digest for document in (conversion, forcefield) for key, digest in parents.items())
            or any(conversion.get(key) != audit[key] for key in ("forcefield", "water", "simulation_state"))
            or minimization.get("schema") != "openmm-minimization-artifact-v1"
            or minimization.get("status") != "BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING"
            or minimization.get("parents") != {"parameter_audit_sha256": parent["manifest_sha256"],
                **{key: expected_stages[key] for key in ("conversion_manifest_sha256", "forcefield_audit_manifest_sha256")}}
            or minimization.get("platform") != "CPU"
            or minimization.get("max_iterations") != spec.parameters["max_minimization_iterations"]
            or minimization.get("convergence_claimed") is not False):
        raise ValueError("Preparation stage contracts or parent hashes are inconsistent")
    for document, directory, names in (
        (conversion, "conversion", {"unique.cms", "title-normalization.json", "atomtype-elements.json", "system.top", "system.gro", "source-atom-arrays.npz"}),
        (minimization, "minimization", {"system.xml", "minimized-positions-nm.npy", "cpu-reference-forces.npy"}),
    ):
        if not isinstance(document.get("artifacts"), dict) or set(document["artifacts"]) != names:
            raise ValueError("Preparation artifact inventory is incomplete")
        for name in names:
            digest = document["artifacts"][name]
            if not isinstance(digest, str):
                raise ValueError("Preparation artifact checksum is missing")
            file(directory + "/" + name, digest)
    if not isinstance(forcefield.get("system_xml_sha256"), str):
        raise ValueError("Audited system checksum is missing")
    file("forcefield-audit/system.xml", forcefield["system_xml_sha256"])
    for key in ("initial_energy_kj_mol", "final_energy_kj_mol", "initial_force_rms_kj_mol_nm",
                "final_force_rms_kj_mol_nm", "maximum_atom_displacement_angstrom", "covalent_distance_angstrom"):
        number = minimization.get(key)
        if type(number) not in (int, float) or not math.isfinite(number):
            raise ValueError("Minimization metrics are missing or nonfinite")
    if (minimization["final_energy_kj_mol"] >= minimization["initial_energy_kj_mol"]
            or not 1.5 < minimization["covalent_distance_angstrom"] < 2.2):
        raise ValueError("Minimization receipt contradicts its bounded gate")
    return {"run_id": spec.run_id, "manifest_sha256": checksum, "candidate_id": parent["candidate_id"],
        "parameterization_completed": True, "independent_forcefield_audit_completed": True,
        "bounded_minimization_completed": True, "short_qc_completed": False,
        "md_system_ready": False, "simulation_started": False}
