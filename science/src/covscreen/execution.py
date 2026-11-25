from __future__ import annotations

from .layout import (enable_library_first, library_script, resolve_route_config,
                     science_script, config_reference)

import json
import hashlib
import math
import os
import re
import subprocess
import sys
import time
import uuid
import platform
from pathlib import Path
from typing import Any

import yaml


class NativeAdductRuntimeUnavailable(ValueError):
    """Registered graph-QC runtime is unavailable, not a molecular rejection."""

from .jobs import (
    ArtifactRecord,
    ArtifactRegistry,
    JobSpec,
    JobSubmissionRequest,
    ResourceBudget,
    RunResult,
    sha256_file,
    utc_now,
)


def _safe_run_id(engine: str) -> str:
    timestamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{timestamp}-{engine}-{uuid.uuid4().hex[:10]}"


def _isolated_python_environment(*pythonpaths: Path) -> dict[str, str]:
    """Build a registered scientific Python environment without shell leakage."""
    environment = {key: value for key, value in os.environ.items()
                   if key in {"HOME", "PATH", "LANG", "LC_ALL", "CUDA_VISIBLE_DEVICES"}}
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
                       PYTHONPATH=os.pathsep.join(str(path) for path in pythonpaths))
    return environment


def _check_openmm_entrypoint(python: Path, script: Path, pythonpath: Path):
    """Check the real CLI imports in its worker environment, without computation."""
    probe = subprocess.run([str(python), '-B', str(script), '--help'],
        env=_isolated_python_environment(pythonpath), capture_output=True, text=True,
        timeout=30, check=False)
    if probe.returncode != 0:
        lines = probe.stderr.strip().splitlines()
        raise ValueError('Registered OpenMM entrypoint is unavailable: '
                         + (lines[-1] if lines else 'exit ' + str(probe.returncode)))


class JobManager:
    """Approved, registered-engine execution manager for OpenScience MCP."""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self.registry = ArtifactRegistry(self.project_root)
        config_path = self.project_root / "configs" / "local.inspire.yaml"
        self._configuration_bytes = config_path.read_bytes() if config_path.is_file() else None
        self.config = yaml.safe_load(self._configuration_bytes) if self._configuration_bytes is not None else {}

    def _within_project(self, value: str, *, must_exist: bool = True) -> Path:
        path = Path(value)
        path = path.resolve() if path.is_absolute() else (self.project_root / path).resolve()
        if must_exist and path.suffix in {'.yaml', '.yml'}:
            path = resolve_route_config(self.project_root, value)
        else:
            path.relative_to(self.project_root)
        if must_exist and not path.exists():
            raise ValueError(f"required project path does not exist: {value}")
        return path

    def equilibration_runtime(self, parameters):
        """An optional Ligand-first registration must not change Library-first."""
        if str(parameters.get('workflow_id', '')).startswith('ligand-flow-'):
            return self.config.get('ligand_openmm_equilibration', self.config.get('openmm_equilibration', {}))
        return self.config.get('openmm_equilibration', {})

    def worker_backend(self, request: JobSubmissionRequest) -> str:
        from .vendor_budget import require_budget_control
        runtime = self.config.get('runtime', {})
        if request.parameters.get('dispatch_policy') == 'library-lo-instance-v1':
            if (request.route != 'library-first' or request.engine != 'schrodinger-covdock-lo'
                    or request.parameters.get('workflow_id') != request.project_id
                    or not request.project_id.startswith('library-flow-')
                    or request.parameters.get('execution_instance') != 'asgbie2'):
                raise ValueError('Library LO instance policy requires its bound Library workflow')
            if request.execution_mode != 'plan' and runtime.get('enable_library_lo_instance') is not True:
                raise ValueError('The Library instance LO executor is not enabled')
            return 'external-instance'
        if request.engine == 'de-novo-prereaction-glide':
            if request.route != 'de-novo' or request.parameters.get('execution_instance') != 'asgbie3':
                raise ValueError('Independent Glide requires its De novo instance binding')
            if request.execution_mode != 'plan' and runtime.get('enable_denovo_glide_instance') is not True:
                raise ValueError('The De novo instance Glide executor is not enabled')
            return 'external-instance'
        if request.parameters.get('dispatch_policy') == 'denovo-batch-lo-instance-v1':
            if (request.route != 'de-novo' or request.engine != 'schrodinger-covdock-lo'
                    or not request.parameters.get('denovo_batch_lo_sha256')
                    or request.parameters.get('execution_instance') != 'asgbie3'):
                raise ValueError('Batch LO instance policy requires its bound De novo source')
            if request.execution_mode != 'plan' and runtime.get('enable_denovo_lo_instance') is not True:
                raise ValueError('The De novo instance LO executor is not enabled')
            return 'external-instance'
        if request.parameters.get('dispatch_policy') == 'ligand-lo-instance-v1':
            from .ligand_instance import selected_instance
            if (request.route != 'ligand-first' or request.engine != 'schrodinger-covdock-lo'
                    or not request.parameters.get('workflow_id')
                    or request.parameters.get('execution_instance') != selected_instance()):
                raise ValueError('LO instance policy requires its bound Ligand workflow')
            if request.execution_mode != 'plan' and runtime.get('enable_ligand_lo_instance') is not True:
                raise ValueError('The instance LO executor is not enabled')
            return 'external-instance'
        if request.parameters.get('dispatch_policy') == 'library-vs-lo-v1':
            if request.route != 'library-first' or request.engine != 'schrodinger-covdock-lo' or not request.parameters.get('workflow_id'):
                raise ValueError('Library LO policy requires its bound native workflow')
            return 'external-hpc'
        if request.engine == 'schrodinger-covdock-vs':
            packet = request.parameters.get('vs_preparation')
            if isinstance(packet, dict):
                plan = packet.get('plan')
                if packet.get('worker_backend') == 'external-instance' or (
                    isinstance(plan, dict) and plan.get('execution_instance') is not None
                ):
                    if request.route == 'library-first':
                        if (packet.get('worker_backend') == 'external-instance'
                                and isinstance(plan, dict) and plan.get('execution_instance') == 'asgbie2'
                                and (request.execution_mode == 'plan'
                                     or runtime.get('enable_library_vs_instance') is True)):
                            return 'external-instance'
                        raise ValueError('The Library instance VS executor is not enabled or bound to asgbie2')
                    from .ligand_instance import selected_instance
                    expected_instance = selected_instance() if request.route == 'ligand-first' else 'asgbie3'
                    enabled = (request.route == 'ligand-first' and os.environ.get('COVALENT_LIGAND_VS_INSTANCE') == expected_instance
                               or request.route == 'de-novo' and runtime.get('enable_denovo_vs_instance') is True)
                    if ((request.execution_mode == 'plan' or enabled)
                            and request.route in {'ligand-first', 'de-novo'}
                            and packet.get('worker_backend') == 'external-instance'
                            and isinstance(plan, dict) and plan.get('execution_instance') == expected_instance):
                        return 'external-instance'
                    raise ValueError('The instance VS executor is not enabled')
            return 'external-hpc'
        if request.engine == 'covindb-prescreen' and request.parameters.get('vs_prescreen'):
            return 'external-hpc'
        if request.parameters.get('dispatch_policy') == 'denovo-independent-local-v1':
            if (request.route != 'de-novo' or request.engine != 'schrodinger-covdock-lo'
                    or not request.parameters.get('workflow_id')):
                raise ValueError('De novo local policy is reserved for its independent CovDock review')
            return require_budget_control(request, 'local')
        if (request.engine == 'library-first-openmm-equilibration'
                and self.equilibration_runtime(request.parameters).get('platform') == 'CUDA'):
            return 'external-openmm'
        # Evaluation and registered adduct preparation are CPU stages.
        # A project may select external-hpc as its
        # default for CovDock, but that dispatcher intentionally accepts only
        # CovDock jobs.  Keep downstream registered execution local unless a
        # dedicated post-processing backend is configured explicitly.
        if request.engine in {
            "de-novo-prereaction-vina",
            "library-first-receptor-preparation",
            "ligand-first-evaluation",
            "ligand-first-pareto",
            "library-first-evaluation",
            "library-first-pareto",
            "library-first-md-candidate-delivery",
            "library-first-md-adduct-admission",
            "library-first-adduct-parameterization",
            "library-first-openmm-system-preparation",
            "library-first-openmm-short-qc",
            "library-first-openmm-equilibration",
        }:
            return runtime.get("postprocessing_worker_backend", "local")
        if request.parameters.get('dispatch_policy') == 'ligand-smoke-v1':
            if runtime.get('enable_ligand_smoke_dispatch') is not True:
                raise ValueError('Bounded Ligand-first smoke dispatch is not enabled')
            return 'external-inspire'
        if request.parameters.get('dispatch_policy') in {'ligand-hpc-sharded-v1', 'ligand-hpc-retained-v1'}:
            if runtime.get('enable_ligand_hpc_sharded_dispatch') is not True:
                raise ValueError('Bounded Ligand-first sharded HPC dispatch is not enabled')
            return 'external-hpc'
        return require_budget_control(request, runtime.get('worker_backend', 'local'))

    def _python(self) -> str:
        configured = self.config.get("carsidock", {}).get("python")
        if configured and Path(configured).is_file():
            return configured
        return sys.executable

    def _bundled_vendor(self, name: str) -> Path:
        # Vendor code is part of the immutable source release.  The project
        # root may be a data-only campaign directory selected through
        # COVSCREEN_PROJECT_ROOT and must not contain a mutable second copy.
        source_root = Path(__file__).resolve().parents[2]
        path = (source_root / "vendor" / name).resolve()
        if not path.is_relative_to(source_root) or path.is_symlink() or not path.is_dir():
            raise ValueError(f"Bundled {name} source is unavailable")
        return path

    def _runtime_pythonpath(self, runtime: dict[str, Any], key: str = "pythonpath") -> Path:
        path = Path(str(runtime.get(key, ""))).expanduser().resolve()
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Registered scientific Python path is unavailable")
        return path

    def _native_adduct_runtime(self) -> dict[str, Any]:
        """Validate the single registered Schrodinger runtime used by adduct stages."""
        runtime=self.config.get("adduct_parameterization",{})
        if runtime.get("enabled") is not True:
            raise NativeAdductRuntimeUnavailable("Registered native adduct runtime is disabled")
        values={"audit_python":Path(str(runtime.get("audit_python",""))).expanduser(),
            "launcher":Path(str(runtime.get("launcher",""))).expanduser(),
            "vendor_root":Path(str(runtime.get("vendor_root",""))).expanduser().resolve(),
            "license_file":Path(str(runtime.get("license_file",""))).expanduser().resolve(),
            "runtime_lib_dir":Path(str(runtime.get("runtime_lib_dir",""))).expanduser().resolve()}
        if (not values["audit_python"].is_file() or not os.access(values["audit_python"],os.X_OK)
                or not values["launcher"].is_file() or not os.access(values["launcher"],os.X_OK)
                or not values["vendor_root"].is_dir() or not values["license_file"].is_file()
                or not values["runtime_lib_dir"].is_dir()):
            raise NativeAdductRuntimeUnavailable("Registered Schrodinger runtime assets are unavailable")
        environment={key:value for key,value in os.environ.items()
            if key in {"HOME","PATH","LANG","LC_ALL"}}
        environment.update(SCHRODINGER=str(values["vendor_root"]),
            SCHRODINGER_LICENSE_FILE=str(values["license_file"]),
            LD_LIBRARY_PATH=str(values["runtime_lib_dir"]),PYTHONDONTWRITEBYTECODE="1")
        try:
            probe=subprocess.run([str(values["launcher"]),"python3","-B","-c",
                "import json,schrodinger; print(json.dumps({'runtime':'schrodinger-python','module':schrodinger.__name__}))"],
                capture_output=True,text=True,timeout=30,check=False,env=environment)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise NativeAdductRuntimeUnavailable('Registered native runtime probe unavailable: ' + str(error)) from error
        try: version=json.loads(probe.stdout) if probe.returncode==0 else None
        except json.JSONDecodeError: version=None
        if version!={"runtime":"schrodinger-python","module":"schrodinger"}:
            detail=probe.stderr.strip().splitlines()
            raise NativeAdductRuntimeUnavailable("Registered Schrodinger runtime probe failed: "+
                (detail[-1] if detail else f"exit {probe.returncode}"))
        values["probe"]=version
        return values

    def _build_deepcosi_site_prediction(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        from .receptor_input import load_receptor_preparation_input
        required = {"receptor_input_artifact_id", "manifest_sha256", "native_timeout_seconds"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError("shared-deepcosi-site-prediction missing parameters: " + ", ".join(missing))
        loaded = load_receptor_preparation_input(self.project_root,
            str(parameters["receptor_input_artifact_id"]), str(parameters["manifest_sha256"]))
        runtime = self.config.get("deepcosi", {})
        if runtime.get("enabled") is not True:
            raise ValueError("Registered DeepCoSI runtime is disabled")
        paths = {name: Path(str(runtime.get(name, ""))).expanduser().resolve()
                 for name in ("python", "runner", "repository", "chimera_bin")}
        entry = paths["repository"] / "codes/DeepCoSI_prediction.py"
        model = paths["repository"] / "codes/DeepCoSI_model_v1.pth"
        chimera = paths["chimera_bin"] / "chimera"
        if (not paths["python"].is_file() or not os.access(paths["python"], os.X_OK)
                or not paths["runner"].is_file() or not paths["repository"].is_dir()
                or not paths["chimera_bin"].is_dir() or not chimera.is_file()
                or not entry.is_file() or not model.is_file()):
            raise ValueError("Registered DeepCoSI runtime assets are unavailable")
        expected_entry = str(runtime.get("entry_sha256", ""))
        expected_model = str(runtime.get("model_sha256", ""))
        expected_runner = str(runtime.get("runner_sha256", ""))
        expected_chimera = str(runtime.get("chimera_sha256", ""))
        if (sha256_file(entry) != expected_entry or sha256_file(model) != expected_model
                or sha256_file(paths["runner"]) != expected_runner
                or sha256_file(chimera) != expected_chimera):
            raise ValueError("Registered DeepCoSI runtime asset hash differs")
        parameters["runtime_registration"] = {
            "python": str(paths["python"]), "runner": str(paths["runner"]),
            "repository": str(paths["repository"]), "chimera_bin": str(paths["chimera_bin"]),
            "entry_sha256": expected_entry, "model_sha256": expected_model,
            "runner_sha256": expected_runner, "chimera_sha256": expected_chimera,
        }
        output = self.registry.job_dir(run_id) / "artifacts/site-prediction"
        command = [sys.executable, str(science_script("run_deepcosi_site_prediction.py")),
            "--root", str(self.project_root), "--artifact-id", str(parameters["receptor_input_artifact_id"]),
            "--manifest-sha256", str(parameters["manifest_sha256"]), "--output", str(output),
            "--runner", str(paths["runner"]), "--python", str(paths["python"]),
            "--chimera-bin", str(paths["chimera_bin"]), "--entry-sha256", expected_entry,
            "--model-sha256", expected_model, "--timeout", str(parameters["native_timeout_seconds"])]
        return command, [str(path) for path in loaded["evidence_files"]], [str(output)]

    def _build_receptor_preparation(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {
            "receptor_input_artifact_id", "manifest_sha256", "altlocs",
            "heavy_seed", "max_missing_residues",
        }
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"library-first-receptor-preparation missing parameters: {missing}")
        relaxation_version = parameters.get("relaxation_policy_version", "v1")
        if relaxation_version not in ("v1", "v2", "v2-local", "v2-local-bounded", "v2-local-block"):
            raise ValueError("Unregistered receptor relaxation policy version")
        if relaxation_version in {"v2", "v2-local", "v2-local-bounded", "v2-local-block"}:
            from .receptor_relaxation import RELAXATION_POLICY_V2
            from .receptor_local_policy import validate_policy_version, digest
            selected_policy = parameters.get("relaxation_policy")
            if relaxation_version in {"v2-local", "v2-local-bounded", "v2-local-block"}:
                validate_policy_version(selected_policy, relaxation_version)
            elif selected_policy != RELAXATION_POLICY_V2:
                raise ValueError("V2 receptor plan requires the exact registered policy and digest")
            if parameters.get("relaxation_policy_sha256") != digest(selected_policy):
                raise ValueError("Receptor policy digest differs from the exact plan")
        elif "relaxation_policy" in parameters or "relaxation_policy_sha256" in parameters:
            raise ValueError("Explicit receptor policy requires its registered v2 or v2-local version")
        from .receptor_conformer import (plan_alternate_locations,
                                         validate_alternate_location_selection)
        from .receptor_input import load_receptor_preparation_input
        prepared_input = load_receptor_preparation_input(
            self.project_root, str(parameters["receptor_input_artifact_id"]),
            str(parameters["manifest_sha256"]),
        )
        acquisition = prepared_input["acquisition_directory"]
        acquired = prepared_input["acquisition"]
        mapping, = acquired["acquisition"]["mappings"]
        altloc_plan = plan_alternate_locations(
            acquisition / "structure.pdb", mapping,
            acquired["selection"].get("model"),
        )
        validate_alternate_location_selection(altloc_plan, parameters["altlocs"])
        parameters["alternate_location_plan"] = altloc_plan
        evidence = prepared_input["evidence_files"]
        from .receptor_input import ReceptorRuntimeUnavailable
        runtime = self.config.get("receptor_preparation", {})
        if runtime.get("enabled") is not True:
            raise ReceptorRuntimeUnavailable("Registered receptor preparation runtime is disabled")
        # Keep the configured virtual-environment entry points. Resolving their
        # symlinks can bypass pyvenv.cfg and silently drop installed packages.
        audit_python = Path(str(runtime.get("audit_python", ""))).expanduser()
        openmm_python = Path(str(runtime.get("openmm_python", ""))).expanduser()
        try:
            openmm_pythonpath = self._runtime_pythonpath(runtime, "openmm_pythonpath")
        except ValueError as error:
            raise ReceptorRuntimeUnavailable(str(error)) from error
        if runtime.get("pdbfixer_source") != "bundled":
            raise ValueError("Only the bundled, release-hashed PDBFixer source is registered")
        pdbfixer_site = self._bundled_vendor("PDBFixer")
        if not audit_python.is_file() or not os.access(audit_python, os.X_OK):
            raise ReceptorRuntimeUnavailable("Registered receptor audit Python is unavailable")
        if not openmm_python.is_file() or not os.access(openmm_python, os.X_OK):
            raise ReceptorRuntimeUnavailable("Registered receptor OpenMM Python is unavailable")
        if not pdbfixer_site.is_dir():
            raise ReceptorRuntimeUnavailable("Registered PDBFixer package directory is unavailable")
        package_root = Path(__file__).resolve().parents[1]
        probes = {
            "audit": (
                audit_python,
                "import json,rdkit,scipy; print(json.dumps({'python':'audit','rdkit':rdkit.__version__,'scipy':scipy.__version__}))",
                str(package_root),
            ),
            "openmm": (
                openmm_python,
                "import json,openmm,pathlib,pdbfixer; p=pathlib.Path(pdbfixer.__file__).resolve().parents[1]/'PROVENANCE.json'; print(json.dumps({'python':'openmm','openmm':openmm.__version__,'pdbfixer':json.loads(p.read_text())['version']}))",
                os.pathsep.join((str(package_root), str(openmm_pythonpath), str(pdbfixer_site))),
            ),
        }
        versions = {}
        for label, (executable, program, pythonpath) in probes.items():
            try:
                completed = subprocess.run(
                    [str(executable), "-B", "-c", program], capture_output=True,
                    text=True, timeout=30, check=False,
                    env=_isolated_python_environment(*map(Path, pythonpath.split(os.pathsep))),
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ReceptorRuntimeUnavailable('Registered receptor ' + label + ' runtime probe unavailable') from error
            if completed.returncode != 0:
                detail = completed.stderr.strip().splitlines()
                raise ReceptorRuntimeUnavailable(
                    f"Registered receptor {label} runtime probe failed: "
                    + (detail[-1] if detail else f"exit {completed.returncode}")
                )
            try:
                versions[label] = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise ReceptorRuntimeUnavailable(f"Registered receptor {label} runtime returned invalid JSON") from exc
        parameters["runtime_versions"] = versions
        output = self.registry.job_dir(run_id) / "artifacts" / "receptor-preparation"
        command = [
            str(audit_python), "-B",
            str(library_script(self.project_root, "run_receptor_preparation.py")),
            "--project-root", str(self.project_root),
            "--receptor-input-dir", str(acquisition),
            "--manifest-sha256", str(parameters["manifest_sha256"]),
            "--output-dir", str(output),
            "--openmm-python", str(openmm_python),
            "--openmm-pythonpath", str(openmm_pythonpath),
            "--pdbfixer-site-packages", str(pdbfixer_site),
            "--heavy-seed", str(int(parameters["heavy_seed"])),
            "--max-missing-residues", str(int(parameters["max_missing_residues"])),
            "--altlocs-json", json.dumps(parameters["altlocs"], sort_keys=True),
        ]
        if relaxation_version in {"v2", "v2-local", "v2-local-bounded", "v2-local-block"}:
            command.extend(["--relaxation-policy", relaxation_version])
            if relaxation_version in {"v2-local", "v2-local-bounded", "v2-local-block"}:
                if (selected_policy['local_observation_mask']['source_sha256'] != sha256_file(acquisition / 'structure.pdb')
                        or selected_policy['local_observation_mask']['target_entity_position'] != mapping['entity_seq_id']):
                    raise ValueError('Local policy differs from the current receptor source or site')
                command.extend(["--relaxation-policy-json", json.dumps(selected_policy, sort_keys=True, allow_nan=False)])
        return command, [str(path) for path in evidence], [str(output)]

    def _build_adduct_parameterization(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        from covscreen.md_adduct_input import (load_covalent_adduct_directory,
                                               load_covalent_adduct_input)
        registered=all(key in parameters for key in
            ("adduct_admission_run_id","adduct_input_manifest_sha256"))
        legacy=all(key in parameters for key in ("adduct_input_artifact_id","manifest_sha256"))
        if registered==legacy:
            raise ValueError("Adduct parameterization requires exactly one admitted input source")
        if registered:
            parent=str(parameters["adduct_admission_run_id"])
            parent_spec=self.registry.load_spec(parent); parent_result=self.registry.load_result(parent)
            folder=self.registry.job_dir(parent)/"artifacts/md-adduct-admission"
            admission_path=folder/"md-adduct-admission.json"; admitted_dir=folder/"adduct-input"
            if (parent_spec.engine!="library-first-md-adduct-admission"
                    or parent_result.state!="COMPLETED" or parent_result.return_code!=0
                    or admission_path.is_symlink() or not admission_path.is_file()):
                raise ValueError("A completed MD-adduct-admission job is required")
            admission=json.loads(admission_path.read_text())
            admitted=load_covalent_adduct_directory(admitted_dir,
                str(parameters["adduct_input_manifest_sha256"]))
            if (admission.get("status")!="ADDUCT_ADMITTED_PARAMETERIZATION_PENDING"
                    or admission.get("native_graph_qc_completed") is not True
                    or admission.get("artifacts",{}).get("adduct-input/adduct-input.json")
                       !=str(parameters["adduct_input_manifest_sha256"])
                    or admission.get("system_id")!=admitted["manifest"]["system_id"]):
                raise ValueError("MD adduct admission evidence is inconsistent")
            admitted["evidence_files"].extend([self.registry.job_dir(parent)/"spec.json",
                self.registry.job_dir(parent)/"result.json",admission_path])
        else:
            admitted=load_covalent_adduct_input(self.project_root,
                str(parameters["adduct_input_artifact_id"]),str(parameters["manifest_sha256"]))
        runtime=self._native_adduct_runtime()
        audit_python=runtime["audit_python"]; launcher=runtime["launcher"]
        vendor=runtime["vendor_root"]; license_file=runtime["license_file"]
        runtime_lib=runtime["runtime_lib_dir"]; runtime_version=runtime["probe"]
        timeout = int(parameters.get("native_timeout_seconds", 1200))
        if not 60 <= timeout <= 3600:
            raise ValueError("native_timeout_seconds must be in [60, 3600]")
        parameters["system_id"] = admitted["manifest"]["system_id"]
        parameters["reaction_chemistry"] = admitted["manifest"]["chemistry"]
        parameters["runtime_registration"] = {
            "launcher": str(launcher), "vendor_root": str(vendor),
            "runtime_lib_dir": str(runtime_lib), "license_path_registered": True,
            "probe": runtime_version,
        }
        output = self.registry.job_dir(run_id) / "artifacts/adduct-parameterization"
        command = [str(audit_python), "-B",
            str(library_script(self.project_root, "run_adduct_parameterization_job.py")),
            "--launcher", str(launcher), "--vendor-root", str(vendor),
            "--license-file", str(license_file), "--runtime-lib-dir", str(runtime_lib),
            "--source", str(admitted["directory"] / "adduct.mae"),
            "--qc", str(admitted["directory"] / "graph-qc.json"),
            "--qc-sha256", sha256_file(admitted["directory"] / "graph-qc.json"),
            "--expected-system-id", admitted["manifest"]["system_id"],
            "--output", str(output), "--timeout-seconds", str(timeout)]
        return command, [str(path) for path in admitted["evidence_files"]], [str(output)]

    def _build_openmm_system_preparation(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"parameterization_run_id", "parameterization_audit_sha256"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"library-first-openmm-system-preparation missing parameters: {missing}")
        parent_id = str(parameters["parameterization_run_id"])
        parent_spec = self.registry.load_spec(parent_id)
        parent_result = self.registry.load_result(parent_id)
        if (parent_spec.engine != "library-first-adduct-parameterization"
                or parent_result.engine != parent_spec.engine
                or parent_result.state != "COMPLETED" or parent_result.return_code != 0):
            raise ValueError("A completed adduct-parameterization job is required")
        parent = self.registry.job_dir(parent_id) / "artifacts/adduct-parameterization"
        audit_path = parent / "parameterization-audit.json"
        source = parent / "parameterized.cms"
        mapping = parent / "atom-mapping.json"
        finished = parent / "finished.json"
        evidence = [self.registry.job_dir(parent_id) / "spec.json",
            self.registry.job_dir(parent_id) / "result.json", audit_path, source, mapping, finished]
        if any(path.is_symlink() or not path.is_file() for path in evidence):
            raise ValueError("Completed parameterization evidence is incomplete")
        if sha256_file(audit_path) != str(parameters["parameterization_audit_sha256"]):
            raise ValueError("Parameterization audit checksum mismatch")
        audit = json.loads(audit_path.read_text())
        completion = json.loads(finished.read_text())
        if (audit.get("status") != "COVALENT_PARAMETERIZATION_AUDITED"
                or audit.get("parameters_audited") is not True
                or audit.get("simulation_started") is not False
                or audit.get("parameterized_cms_sha256") != sha256_file(source)
                or audit.get("atom_mapping_sha256") != sha256_file(mapping)
                or completion.get("status") != audit.get("status")
                or completion.get("parameters_audited") is not True
                or completion.get("system_id") != audit.get("system_id")
                or completion.get("parameterized_cms_sha256") != audit.get("parameterized_cms_sha256")
                or completion.get("atom_mapping_sha256") != audit.get("atom_mapping_sha256")):
            raise ValueError("Parameterization evidence has not passed the required state")
        runtime = self.config.get("openmm_system_preparation", {})
        if runtime.get("enabled") is not True:
            raise ValueError("Registered OpenMM system-preparation runtime is disabled")
        python = Path(str(runtime.get("python", ""))).expanduser()
        runtime_pythonpath = self._runtime_pythonpath(runtime)
        if runtime.get("intermol_source") != "bundled":
            raise ValueError("Only the bundled, release-hashed InterMol source is registered")
        intermol = self._bundled_vendor("InterMol")
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError("Registered OpenMM preparation Python is unavailable")
        if (intermol.is_symlink() or not intermol.is_dir()
                or not (intermol / "intermol/desmond/desmond_parser.py").is_file()):
            raise ValueError("Registered InterMol source is unavailable")
        probe = subprocess.run([str(python), "-B", "-c",
            "import json,openmm,numpy,parmed; from openmm import app,unit; "
            "import parmed.unit; from intermol.desmond import desmond_parser; "
            "from intermol.forces.constraint import Constraint; "
            "from intermol.gromacs import gromacs_parser; "
            "print(json.dumps({'openmm':openmm.__version__,'numpy':numpy.__version__,'parmed':parmed.__version__}))"],
            capture_output=True, text=True, timeout=30, check=False,
            env=_isolated_python_environment(runtime_pythonpath, intermol))
        try:
            versions = json.loads(probe.stdout) if probe.returncode == 0 else None
        except json.JSONDecodeError:
            versions = None
        if not isinstance(versions, dict) or set(versions) != {"openmm", "numpy", "parmed"}:
            detail = probe.stderr.strip().splitlines()
            raise ValueError("Registered OpenMM preparation runtime probe failed: "
                             + (detail[-1] if detail else f"exit {probe.returncode}"))
        max_iterations = int(parameters.get("max_minimization_iterations", 100))
        if not 1 <= max_iterations <= 1000:
            raise ValueError("max_minimization_iterations must be in [1, 1000]")
        parameters["system_id"] = audit["system_id"]
        parameters["runtime_versions"] = versions
        parameters["runtime_pythonpath"] = str(runtime_pythonpath)
        parameters["source_cms_sha256"] = audit["parameterized_cms_sha256"]
        output = self.registry.job_dir(run_id) / "artifacts/openmm-system-preparation"
        script = library_script(self.project_root, "run_openmm_system_preparation_job.py")
        _check_openmm_entrypoint(python, script, runtime_pythonpath)
        command = [str(python), "-B",
            str(script),
            "--source", str(source), "--source-sha256", audit["parameterized_cms_sha256"],
            "--parameter-audit", str(audit_path),
            "--parameter-audit-sha256", str(parameters["parameterization_audit_sha256"]),
            "--intermol-source", str(intermol), "--output", str(output),
            "--cpu-threads", str(int(parameters.get("cpu_threads", 2))),
            "--max-iterations", str(max_iterations)]
        return command, [str(path) for path in evidence], [str(output)]

    def _build_openmm_short_qc(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"system_preparation_run_id", "preparation_manifest_sha256"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"library-first-openmm-short-qc missing parameters: {missing}")
        parent_id = str(parameters["system_preparation_run_id"])
        parent_spec = self.registry.load_spec(parent_id)
        parent_result = self.registry.load_result(parent_id)
        if (parent_spec.engine != "library-first-openmm-system-preparation"
                or parent_result.engine != parent_spec.engine
                or parent_result.state != "COMPLETED" or parent_result.return_code != 0):
            raise ValueError("A completed OpenMM system-preparation job is required")
        folder = self.registry.job_dir(parent_id) / "artifacts/openmm-system-preparation"
        preparation_path = folder / "openmm-preparation.json"
        conversion_path = folder / "conversion/conversion.json"
        forcefield_path = folder / "forcefield-audit/forcefield-audit.json"
        minimization_path = folder / "minimization/minimization.json"
        minimization_system = folder / "minimization/system.xml"
        minimization_positions = folder / "minimization/minimized-positions-nm.npy"
        minimization_forces = folder / "minimization/cpu-reference-forces.npy"
        parameterization_run_id = parent_spec.parent_run_id
        if not parameterization_run_id:
            raise ValueError("System-preparation job lacks its parameterization parent")
        parameter_audit = (self.registry.job_dir(parameterization_run_id)
            / "artifacts/adduct-parameterization/parameterization-audit.json")
        evidence = [self.registry.job_dir(parent_id) / "spec.json",
            self.registry.job_dir(parent_id) / "result.json", preparation_path,
            conversion_path, forcefield_path, minimization_path, minimization_system,
            minimization_positions, minimization_forces, parameter_audit]
        if any(path.is_symlink() or not path.is_file() for path in evidence):
            raise ValueError("Completed OpenMM preparation evidence is incomplete")
        if sha256_file(preparation_path) != str(parameters["preparation_manifest_sha256"]):
            raise ValueError("OpenMM preparation manifest checksum mismatch")
        preparation = json.loads(preparation_path.read_text())
        conversion = json.loads(conversion_path.read_text())
        forcefield = json.loads(forcefield_path.read_text())
        minimization = json.loads(minimization_path.read_text())
        parameter_sha = sha256_file(parameter_audit)
        if (preparation.get("status") != "OPENMM_SYSTEM_MINIMIZED_SHORT_QC_PENDING"
                or preparation.get("short_qc_completed") is not False
                or preparation.get("simulation_started") is not False
                or preparation.get("parents", {}).get("parameter_audit_sha256") != parameter_sha
                or preparation.get("stages", {}).get("conversion_manifest_sha256") != sha256_file(conversion_path)
                or preparation.get("stages", {}).get("forcefield_audit_manifest_sha256") != sha256_file(forcefield_path)
                or preparation.get("stages", {}).get("minimization_manifest_sha256") != sha256_file(minimization_path)
                or len({preparation.get("system_id"), conversion.get("system_id"),
                        forcefield.get("system_id"), minimization.get("system_id")}) != 1):
            raise ValueError("OpenMM preparation evidence has not passed the required state")
        runtime = self.config.get("openmm_system_preparation", {})
        if runtime.get("enabled") is not True:
            raise ValueError("Registered OpenMM runtime is disabled")
        python = Path(str(runtime.get("python", ""))).expanduser()
        runtime_pythonpath = self._runtime_pythonpath(runtime)
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError("Registered OpenMM Python is unavailable")
        probe = subprocess.run([str(python), "-B", "-c",
            "import json,openmm,numpy; print(json.dumps({'openmm':openmm.__version__,'numpy':numpy.__version__}))"],
            capture_output=True, text=True, timeout=30, check=False,
            env=_isolated_python_environment(runtime_pythonpath))
        try:
            versions = json.loads(probe.stdout) if probe.returncode == 0 else None
        except json.JSONDecodeError:
            versions = None
        if not isinstance(versions, dict) or set(versions) != {"openmm", "numpy"}:
            raise ValueError("Registered OpenMM short-QC runtime probe failed")
        parameters.update(system_id=preparation["system_id"], runtime_versions=versions,
                          runtime_pythonpath=str(runtime_pythonpath),
                          parameterization_run_id=parameterization_run_id)
        output = self.registry.job_dir(run_id) / "artifacts/openmm-short-qc"
        script = library_script(self.project_root, "run_openmm_short_qc.py")
        _check_openmm_entrypoint(python, script, runtime_pythonpath)
        command = [str(python), "-B", str(script),
            "--parameter-audit", str(parameter_audit), "--parameter-audit-sha256", parameter_sha,
            "--conversion-directory", str(folder / "conversion"),
            "--conversion-manifest-sha256", sha256_file(conversion_path),
            "--forcefield-audit-directory", str(folder / "forcefield-audit"),
            "--forcefield-audit-manifest-sha256", sha256_file(forcefield_path),
            "--minimization-directory", str(folder / "minimization"),
            "--minimization-manifest-sha256", sha256_file(minimization_path),
            "--output", str(output), "--cpu-threads", str(parameters["cpu_threads"]),
            "--steps", str(parameters["steps"]), "--report-interval", str(parameters["report_interval"]),
            "--velocity-seed", str(parameters["velocity_seed"])]
        return command, [str(path) for path in evidence], [str(output)]

    def _build_openmm_equilibration(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"short_qc_run_id", "short_qc_manifest_sha256"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"library-first-openmm-equilibration missing parameters: {missing}")
        short_run = str(parameters["short_qc_run_id"])
        short_spec = self.registry.load_spec(short_run)
        short_result = self.registry.load_result(short_run)
        if (short_spec.engine != "library-first-openmm-short-qc"
                or short_result.engine != short_spec.engine
                or short_result.state != "COMPLETED" or short_result.return_code != 0):
            raise ValueError("A completed OpenMM short-QC job is required")
        short_folder = self.registry.job_dir(short_run) / "artifacts/openmm-short-qc"
        short_manifest = short_folder / "short-qc.json"
        short_positions = short_folder / "final-positions-nm.npy"
        short_velocities = short_folder / "final-velocities-nm-ps.npy"
        preparation_run = short_spec.parent_run_id
        if not preparation_run:
            raise ValueError("Short-QC job lacks its system-preparation parent")
        preparation_spec = self.registry.load_spec(preparation_run)
        if preparation_spec.engine != "library-first-openmm-system-preparation":
            raise ValueError("Short-QC parent is not a system-preparation job")
        parameterization_run = preparation_spec.parent_run_id
        if not parameterization_run:
            raise ValueError("System-preparation job lacks its parameterization parent")
        preparation_folder = (self.registry.job_dir(preparation_run)
            / "artifacts/openmm-system-preparation")
        minimization_folder = preparation_folder / "minimization"
        minimization_manifest = minimization_folder / "minimization.json"
        minimization_system = minimization_folder / "system.xml"
        parameter_audit = (self.registry.job_dir(parameterization_run)
            / "artifacts/adduct-parameterization/parameterization-audit.json")
        evidence = [self.registry.job_dir(short_run)/"spec.json",
            self.registry.job_dir(short_run)/"result.json", short_manifest,
            short_positions, short_velocities, minimization_manifest,
            minimization_system, parameter_audit]
        if any(path.is_symlink() or not path.is_file() for path in evidence):
            raise ValueError("Completed short-QC evidence is incomplete")
        if sha256_file(short_manifest) != str(parameters["short_qc_manifest_sha256"]):
            raise ValueError("Short-QC manifest checksum mismatch")
        short = json.loads(short_manifest.read_text())
        minimization = json.loads(minimization_manifest.read_text())
        parameter_sha = sha256_file(parameter_audit)
        if (short.get("status") != "UNRESTRAINED_SHORT_QC_PASSED_EQUILIBRATION_PENDING"
                or short.get("short_qc_completed") is not True
                or short.get("equilibration_completed") is not False
                or short.get("parents", {}).get("parameter_audit_sha256") != parameter_sha
                or short.get("parents", {}).get("minimization_manifest_sha256") != sha256_file(minimization_manifest)
                or short.get("artifacts", {}).get("final-positions-nm.npy") != sha256_file(short_positions)
                or short.get("artifacts", {}).get("final-velocities-nm-ps.npy") != sha256_file(short_velocities)
                or minimization.get("artifacts", {}).get("system.xml") != sha256_file(minimization_system)
                or short.get("system_id") != minimization.get("system_id")):
            raise ValueError("Short-QC evidence has not passed the required state")
        runtime = self.equilibration_runtime(parameters)
        if runtime.get("enabled") is not True:
            raise ValueError("Registered OpenMM equilibration runtime is disabled")
        runtime_artifacts = runtime.get("runtime_artifacts", {})
        if not isinstance(runtime_artifacts, dict):
            raise ValueError("Registered OpenMM runtime artifacts are invalid")
        for filename, checksum in runtime_artifacts.items():
            path = Path(filename)
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or sha256_file(path) != checksum):
                raise ValueError("Registered OpenMM runtime artifact changed: " + filename)
            evidence.append(path)
        parameters["runtime_artifacts"] = dict(runtime_artifacts)
        python = Path(str(runtime.get("python", ""))).expanduser()
        runtime_pythonpath = self._runtime_pythonpath(runtime)
        platform_name = str(runtime.get("platform", ""))
        properties = dict(runtime.get("platform_properties", {}))
        if (not python.is_file() or not os.access(python, os.X_OK)
                or platform_name not in {"CPU", "CUDA"}
                or not isinstance(properties, dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in properties.items())):
            raise ValueError("Registered OpenMM equilibration runtime is invalid")
        if platform_name == "CPU":
            threads = properties.get("Threads", str(parameters["cpu_threads"]))
            if not str(threads).isdigit() or not 1 <= int(threads) <= parameters["cpu_threads"]:
                raise ValueError("OpenMM Context Threads exceeds the approved CPU budget")
            properties["Threads"] = str(threads)
        script = library_script(self.project_root, "run_openmm_equilibration.py")
        _check_openmm_entrypoint(python, script, runtime_pythonpath)
        # Planning happens on the CPU host. CUDA context validation is a mandatory
        # zero-step part of the separately approved GPU workload, never a claim here.
        probe_platform = 'CPU' if platform_name == 'CUDA' else platform_name
        probe = subprocess.run([str(python), "-B", "-c",
            "import json,openmm,numpy; p=openmm.Platform.getPlatformByName('"+probe_platform+"'); print(json.dumps({'openmm':openmm.__version__,'numpy':numpy.__version__,'platform':p.getName()}))"],
            capture_output=True, text=True, timeout=30, check=False,
            env=_isolated_python_environment(runtime_pythonpath))
        try:
            versions = json.loads(probe.stdout) if probe.returncode == 0 else None
        except json.JSONDecodeError:
            versions = None
        if not isinstance(versions, dict) or versions.get("platform") != probe_platform:
            raise ValueError("Registered OpenMM equilibration runtime probe failed")
        parameters.update(system_id=short["system_id"], runtime_versions=versions,
            runtime_pythonpath=str(runtime_pythonpath),
            platform=platform_name, platform_properties=properties,
            parameterization_run_id=parameterization_run,
            system_preparation_run_id=preparation_run)
        output = self.registry.job_dir(run_id)/"artifacts/openmm-equilibration"
        command = [str(python), "-B", str(library_script(self.project_root,
            "run_openmm_equilibration.py")), "--cpu-threads", str(parameters["cpu_threads"]),
            "--parameter-audit", str(parameter_audit),
            "--parameter-audit-sha256", parameter_sha,
            "--minimization-directory", str(minimization_folder),
            "--minimization-manifest-sha256", sha256_file(minimization_manifest),
            "--short-qc-directory", str(short_folder),
            "--short-qc-manifest-sha256", sha256_file(short_manifest),
            "--output", str(output), "--platform", platform_name,
            "--platform-properties-json", json.dumps(properties, sort_keys=True),
            "--duration-ps", "500", "--report-interval-ps", "10",
            "--integrator-seed", str(parameters["integrator_seed"]),
            "--barostat-seed", str(parameters["barostat_seed"])]
        return command, [str(path) for path in evidence], [str(output)]

    def _build_openmm_production(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"equilibration_run_id", "equilibration_manifest_sha256", "replica"}
        missing = sorted(required-set(parameters))
        if missing:
            raise ValueError(f"library-first-openmm-production missing parameters: {missing}")
        equilibrium_run = str(parameters["equilibration_run_id"])
        equilibrium_spec = self.registry.load_spec(equilibrium_run)
        equilibrium_result = self.registry.load_result(equilibrium_run)
        if (equilibrium_spec.engine != "library-first-openmm-equilibration"
                or equilibrium_result.engine != equilibrium_spec.engine
                or equilibrium_result.state != "COMPLETED" or equilibrium_result.return_code != 0):
            raise ValueError("A completed OpenMM equilibration job is required")
        equilibrium_folder = self.registry.job_dir(equilibrium_run)/"artifacts/openmm-equilibration"
        equilibrium_manifest = equilibrium_folder/"equilibration.json"
        equilibrium_positions = equilibrium_folder/"final-positions-nm.npy"
        equilibrium_box = equilibrium_folder/"final-box-vectors-nm.npy"
        short_run = equilibrium_spec.parent_run_id
        short_spec = self.registry.load_spec(short_run) if short_run else None
        preparation_run = short_spec.parent_run_id if short_spec else None
        preparation_spec = self.registry.load_spec(preparation_run) if preparation_run else None
        parameterization_run = preparation_spec.parent_run_id if preparation_spec else None
        if (not short_spec or short_spec.engine != "library-first-openmm-short-qc"
                or not preparation_spec or preparation_spec.engine != "library-first-openmm-system-preparation"
                or not parameterization_run):
            raise ValueError("Equilibration ancestry is incomplete")
        preparation_folder = self.registry.job_dir(preparation_run)/"artifacts/openmm-system-preparation"
        conversion_folder = preparation_folder/"conversion"
        conversion_manifest = conversion_folder/"conversion.json"
        topology = conversion_folder/"system.top"
        minimization_folder = preparation_folder/"minimization"
        minimization_manifest = minimization_folder/"minimization.json"
        system = minimization_folder/"system.xml"
        parameter_audit = self.registry.job_dir(parameterization_run)/"artifacts/adduct-parameterization/parameterization-audit.json"
        evidence = [self.registry.job_dir(equilibrium_run)/"spec.json",
            self.registry.job_dir(equilibrium_run)/"result.json", equilibrium_manifest,
            equilibrium_positions, equilibrium_box, conversion_manifest, topology,
            minimization_manifest, system, parameter_audit]
        if any(path.is_symlink() or not path.is_file() for path in evidence):
            raise ValueError("Completed equilibration evidence is incomplete")
        if sha256_file(equilibrium_manifest) != str(parameters["equilibration_manifest_sha256"]):
            raise ValueError("Equilibration manifest checksum mismatch")
        equilibrium = json.loads(equilibrium_manifest.read_text())
        conversion = json.loads(conversion_manifest.read_text())
        minimization = json.loads(minimization_manifest.read_text())
        parameter_sha = sha256_file(parameter_audit)
        if (equilibrium.get("status") != "NPT_EQUILIBRATION_COMPLETED_PRODUCTION_REVIEW_PENDING"
                or equilibrium.get("production_review_ready") is not True
                or equilibrium.get("admission_duration_met") is not True
                or equilibrium.get("production_started") is not False
                or equilibrium.get("parents", {}).get("parameter_audit_sha256") != parameter_sha
                or equilibrium.get("parents", {}).get("minimization_manifest_sha256") != sha256_file(minimization_manifest)
                or equilibrium.get("artifacts", {}).get("final-positions-nm.npy") != sha256_file(equilibrium_positions)
                or equilibrium.get("artifacts", {}).get("final-box-vectors-nm.npy") != sha256_file(equilibrium_box)
                or conversion.get("artifacts", {}).get("system.top") != sha256_file(topology)
                or minimization.get("artifacts", {}).get("system.xml") != sha256_file(system)
                or len({equilibrium.get("system_id"), conversion.get("system_id"),
                        minimization.get("system_id")}) != 1):
            raise ValueError("Equilibration evidence has not passed production admission")
        runtime = self.config.get("openmm_production", {})
        if runtime.get("enabled") is not True:
            raise ValueError("Registered OpenMM production runtime is disabled")
        python = Path(str(runtime.get("python", ""))).expanduser()
        runtime_pythonpath = self._runtime_pythonpath(runtime)
        platform_name = str(runtime.get("platform", "")); properties = runtime.get("platform_properties", {})
        if (not python.is_file() or not os.access(python, os.X_OK)
                or platform_name not in {"CPU", "CUDA"} or not isinstance(properties, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k,v in properties.items())):
            raise ValueError("Registered OpenMM production runtime is invalid")
        probe = subprocess.run([str(python), "-B", "-c",
            "import json,openmm,numpy; p=openmm.Platform.getPlatformByName('"+platform_name+"'); print(json.dumps({'openmm':openmm.__version__,'numpy':numpy.__version__,'platform':p.getName()}))"],
            capture_output=True, text=True, timeout=30, check=False,
            env=_isolated_python_environment(runtime_pythonpath))
        try: versions = json.loads(probe.stdout) if probe.returncode == 0 else None
        except json.JSONDecodeError: versions = None
        if not isinstance(versions, dict) or versions.get("platform") != platform_name:
            raise ValueError("Registered OpenMM production runtime probe failed")
        parameters.update(system_id=equilibrium["system_id"], runtime_versions=versions,
            runtime_pythonpath=str(runtime_pythonpath),
            platform=platform_name, platform_properties=properties,
            parameterization_run_id=parameterization_run, system_preparation_run_id=preparation_run,
            short_qc_run_id=short_run)
        output = self.registry.job_dir(run_id)/"artifacts/openmm-production"
        command = [str(python),"-B",str(library_script(self.project_root,"run_openmm_production.py")),
            "--parameter-audit",str(parameter_audit),"--parameter-audit-sha256",parameter_sha,
            "--conversion-directory",str(conversion_folder),"--conversion-manifest-sha256",sha256_file(conversion_manifest),
            "--minimization-directory",str(minimization_folder),"--minimization-manifest-sha256",sha256_file(minimization_manifest),
            "--equilibration-directory",str(equilibrium_folder),"--equilibration-manifest-sha256",sha256_file(equilibrium_manifest),
            "--output",str(output),"--replica",str(parameters["replica"]),
            "--velocity-seed",str(parameters["velocity_seed"]),"--integrator-seed",str(parameters["integrator_seed"]),
            "--barostat-seed",str(parameters["barostat_seed"]),"--platform",platform_name,
            "--platform-properties-json",json.dumps(properties,sort_keys=True)]
        return command,[str(path) for path in evidence],[str(output)]

    def _build_trajectory_audit(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"production_run_id", "production_manifest_sha256"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"library-first-trajectory-audit missing parameters: {missing}")
        production_run = str(parameters["production_run_id"])
        production_spec = self.registry.load_spec(production_run)
        production_result = self.registry.load_result(production_run)
        if (production_spec.engine != "library-first-openmm-production"
                or production_result.engine != production_spec.engine
                or production_result.state != "COMPLETED"
                or production_result.return_code != 0):
            raise ValueError("A completed OpenMM production job is required")
        production_folder = self.registry.job_dir(production_run) / "artifacts/openmm-production"
        production_manifest = production_folder / "production.json"
        trajectory = production_folder / "trajectory.dcd"
        frame_log = production_folder / "frames.jsonl"
        equilibrium_run = production_spec.parent_run_id
        equilibrium_spec = self.registry.load_spec(equilibrium_run) if equilibrium_run else None
        short_run = equilibrium_spec.parent_run_id if equilibrium_spec else None
        short_spec = self.registry.load_spec(short_run) if short_run else None
        preparation_run = short_spec.parent_run_id if short_spec else None
        preparation_spec = self.registry.load_spec(preparation_run) if preparation_run else None
        parameterization_run = preparation_spec.parent_run_id if preparation_spec else None
        if (not equilibrium_spec or equilibrium_spec.engine != "library-first-openmm-equilibration"
                or not short_spec or short_spec.engine != "library-first-openmm-short-qc"
                or not preparation_spec or preparation_spec.engine != "library-first-openmm-system-preparation"
                or not parameterization_run):
            raise ValueError("Production ancestry is incomplete")
        parameter_audit = (self.registry.job_dir(parameterization_run)
            / "artifacts/adduct-parameterization/parameterization-audit.json")
        evidence = [self.registry.job_dir(production_run) / "spec.json",
            self.registry.job_dir(production_run) / "result.json", production_manifest,
            trajectory, frame_log, parameter_audit]
        if any(path.is_symlink() or not path.is_file() for path in evidence):
            raise ValueError("Completed production evidence is incomplete")
        if sha256_file(production_manifest) != str(parameters["production_manifest_sha256"]):
            raise ValueError("Production manifest checksum mismatch")
        production = json.loads(production_manifest.read_text())
        parameter_sha = sha256_file(parameter_audit)
        artifacts = production.get("artifacts", {})
        if (production.get("schema") != "openmm-production-replica-artifact-v1"
                or production.get("status") != "PRODUCTION_REPLICA_COMPLETE_TRAJECTORY_AUDIT_PENDING"
                or production.get("production_completed") is not True
                or production.get("trajectory_audited") is not False
                or production.get("parents", {}).get("parameter_audit_sha256") != parameter_sha
                or artifacts.get("trajectory.dcd") != sha256_file(trajectory)
                or artifacts.get("frames.jsonl") != sha256_file(frame_log)):
            raise ValueError("Production evidence has not passed trajectory-audit admission")
        runtime = self.config.get("openmm_production", {})
        python = Path(str(runtime.get("python", ""))).expanduser()
        runtime_pythonpath = self._runtime_pythonpath(runtime)
        if runtime.get("enabled") is not True or not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError("Registered trajectory-audit Python runtime is invalid")
        probe = subprocess.run([str(python), "-B", "-c",
            "import json,numpy; print(json.dumps({'numpy':numpy.__version__}))"],
            capture_output=True, text=True, timeout=30, check=False,
            env=_isolated_python_environment(runtime_pythonpath))
        try:
            versions = json.loads(probe.stdout) if probe.returncode == 0 else None
        except json.JSONDecodeError:
            versions = None
        if not isinstance(versions, dict) or not versions.get("numpy"):
            raise ValueError("Registered trajectory-audit runtime probe failed")
        parameters.update(system_id=production.get("system_id"), replica=production.get("replica"),
            runtime_versions=versions, runtime_pythonpath=str(runtime_pythonpath),
            parameterization_run_id=parameterization_run,
            equilibrium_run_id=equilibrium_run)
        output = self.registry.job_dir(run_id) / "artifacts/trajectory-audit"
        command = [str(python), "-B", str(library_script(self.project_root,
            "audit_openmm_trajectory.py")), "--parameter-audit", str(parameter_audit),
            "--parameter-audit-sha256", parameter_sha,
            "--production-directory", str(production_folder),
            "--production-manifest-sha256", sha256_file(production_manifest),
            "--output", str(output)]
        return command, [str(path) for path in evidence], [str(output)]

    def _build_structural_analysis(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        parents = parameters.get("trajectory_audits")
        if not isinstance(parents, list) or len(parents) != 3:
            raise ValueError("library-first-structural-analysis requires exactly three audits")
        evidence, replica_arguments, production_specs, production_manifests = [], [], [], []
        for parent in parents:
            if not isinstance(parent, dict) or set(parent) != {"run_id", "manifest_sha256"}:
                raise ValueError("Each trajectory audit requires run_id and manifest_sha256")
            audit_run = str(parent["run_id"])
            audit_spec = self.registry.load_spec(audit_run)
            audit_result = self.registry.load_result(audit_run)
            if (audit_spec.engine != "library-first-trajectory-audit"
                    or audit_result.engine != audit_spec.engine or audit_result.state != "COMPLETED"
                    or audit_result.return_code != 0 or not audit_spec.parent_run_id):
                raise ValueError("A completed formal trajectory-audit job is required")
            audit_path = self.registry.job_dir(audit_run) / "artifacts/trajectory-audit/trajectory-audit.json"
            production_run = audit_spec.parent_run_id
            production_spec = self.registry.load_spec(production_run)
            production_folder = self.registry.job_dir(production_run) / "artifacts/openmm-production"
            production_path = production_folder / "production.json"
            trajectory = production_folder / "trajectory.dcd"
            local = [self.registry.job_dir(audit_run) / "spec.json",
                self.registry.job_dir(audit_run) / "result.json", audit_path,
                production_path, trajectory]
            if any(path.is_symlink() or not path.is_file() for path in local):
                raise ValueError("Trajectory-audit evidence is incomplete")
            if sha256_file(audit_path) != parent["manifest_sha256"]:
                raise ValueError("Trajectory-audit manifest checksum mismatch")
            audit = json.loads(audit_path.read_text())
            production = json.loads(production_path.read_text())
            production_sha = sha256_file(production_path)
            if (production_spec.engine != "library-first-openmm-production"
                    or audit.get("status") != "TRAJECTORY_INTEGRITY_VERIFIED_ANALYSIS_PENDING"
                    or audit.get("parents", {}).get("production_manifest_sha256") != production_sha
                    or production.get("status") != "PRODUCTION_REPLICA_COMPLETE_TRAJECTORY_AUDIT_PENDING"):
                raise ValueError("Trajectory audit has not passed structural-analysis admission")
            evidence.extend(local); production_specs.append(production_spec); production_manifests.append(production)
            replica_arguments.append({"production_directory": str(production_folder),
                "production_manifest_sha256": production_sha, "trajectory_audit": str(audit_path),
                "trajectory_audit_sha256": sha256_file(audit_path)})
        if {item.get("replica") for item in production_manifests} != {1, 2, 3}:
            raise ValueError("Formal replicas 1, 2 and 3 are all required")
        equilibrium_runs = {spec.parent_run_id for spec in production_specs}
        if len(equilibrium_runs) != 1 or None in equilibrium_runs:
            raise ValueError("Production replicas must share one equilibration job")
        equilibrium_run = next(iter(equilibrium_runs)); equilibrium_spec = self.registry.load_spec(equilibrium_run)
        short_spec = self.registry.load_spec(equilibrium_spec.parent_run_id) if equilibrium_spec.parent_run_id else None
        preparation_spec = self.registry.load_spec(short_spec.parent_run_id) if short_spec and short_spec.parent_run_id else None
        parameterization_run = preparation_spec.parent_run_id if preparation_spec else None
        if (equilibrium_spec.engine != "library-first-openmm-equilibration" or not short_spec
                or short_spec.engine != "library-first-openmm-short-qc" or not preparation_spec
                or preparation_spec.engine != "library-first-openmm-system-preparation" or not parameterization_run):
            raise ValueError("Three-replica ancestry is incomplete")
        parameter_folder = self.registry.job_dir(parameterization_run) / "artifacts/adduct-parameterization"
        parameter_audit = parameter_folder / "parameterization-audit.json"
        atom_mapping = parameter_folder / "atom-mapping.json"
        minimization_folder = (self.registry.job_dir(preparation_spec.run_id)
            / "artifacts/openmm-system-preparation/minimization")
        minimization_manifest = minimization_folder / "minimization.json"
        reference = minimization_folder / "minimized-positions-nm.npy"
        base = [parameter_audit, atom_mapping, minimization_manifest, reference]
        if any(path.is_symlink() or not path.is_file() for path in base):
            raise ValueError("Structural-analysis reference evidence is incomplete")
        parameter = json.loads(parameter_audit.read_text())
        minimization = json.loads(minimization_manifest.read_text())
        if (parameter.get("atom_mapping_sha256") != sha256_file(atom_mapping)
                or minimization.get("artifacts", {}).get("minimized-positions-nm.npy") != sha256_file(reference)
                or any(item.get("system_id") != parameter.get("system_id")
                       or item.get("parents", {}).get("parameter_audit_sha256") != sha256_file(parameter_audit)
                       for item in production_manifests)):
            raise ValueError("Three-replica reference evidence is inconsistent")
        evidence.extend(base)
        runtime = self.config.get("openmm_production", {}); python = Path(str(runtime.get("python", ""))).expanduser()
        runtime_pythonpath = self._runtime_pythonpath(runtime)
        if runtime.get("enabled") is not True or not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError("Registered structural-analysis Python runtime is invalid")
        parameters.update(system_id=production_manifests[0].get("system_id"), replicas=[1, 2, 3],
            runtime_pythonpath=str(runtime_pythonpath), equilibrium_run_id=equilibrium_run,
            parameterization_run_id=parameterization_run)
        output = self.registry.job_dir(run_id) / "artifacts/structural-analysis"
        command = [str(python), "-B", str(library_script(self.project_root, "analyze_openmm_replicas.py")),
            "--parameter-audit", str(parameter_audit), "--parameter-audit-sha256", sha256_file(parameter_audit),
            "--atom-mapping", str(atom_mapping), "--atom-mapping-sha256", sha256_file(atom_mapping),
            "--reference-positions", str(reference), "--reference-positions-sha256", sha256_file(reference),
            "--output", str(output)]
        for item in replica_arguments:
            command.extend(["--replica-json", json.dumps(item, sort_keys=True)])
        return command, [str(path) for path in evidence], [str(output)]

    def _build_md_report(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"structural_analysis_run_id", "structural_analysis_manifest_sha256"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"library-first-md-report missing parameters: {missing}")
        parent_run = str(parameters["structural_analysis_run_id"])
        parent_spec = self.registry.load_spec(parent_run)
        parent_result = self.registry.load_result(parent_run)
        if (parent_spec.engine != "library-first-structural-analysis"
                or parent_result.engine != parent_spec.engine
                or parent_result.state != "COMPLETED" or parent_result.return_code != 0):
            raise ValueError("A completed structural-analysis job is required")
        folder = self.registry.job_dir(parent_run) / "artifacts/structural-analysis"
        manifest = folder / "structural-analysis.json"
        frame_metrics = folder / "frame-metrics.csv"
        evidence = [self.registry.job_dir(parent_run) / "spec.json",
            self.registry.job_dir(parent_run) / "result.json", manifest, frame_metrics]
        if any(path.is_symlink() or not path.is_file() for path in evidence):
            raise ValueError("Structural-analysis evidence is incomplete")
        if sha256_file(manifest) != str(parameters["structural_analysis_manifest_sha256"]):
            raise ValueError("Structural-analysis manifest checksum mismatch")
        analysis = json.loads(manifest.read_text())
        if (analysis.get("schema") != "openmm-three-replica-structural-analysis-v1"
                or analysis.get("status") != "THREE_REPLICA_STRUCTURAL_ANALYSIS_COMPLETE_REPORT_PENDING"
                or analysis.get("replicas") != [1, 2, 3]
                or analysis.get("screening_decision_made") is not False
                or analysis.get("artifacts", {}).get("frame-metrics.csv") != sha256_file(frame_metrics)):
            raise ValueError("Structural analysis has not passed MD-report admission")
        runtime = self.config.get("openmm_production", {}); python = Path(str(runtime.get("python", ""))).expanduser()
        runtime_pythonpath = self._runtime_pythonpath(runtime)
        if runtime.get("enabled") is not True or not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError("Registered MD-report Python runtime is invalid")
        parameters.update(system_id=analysis.get("system_id"),
            runtime_pythonpath=str(runtime_pythonpath),
            policy_id="library-first-covalent-md-structural-triage-v1")
        output = self.registry.job_dir(run_id) / "artifacts/md-report"
        command = [str(python), "-B", str(library_script(self.project_root,
            "build_candidate_md_report.py")), "--structural-analysis", str(manifest),
            "--structural-analysis-sha256", sha256_file(manifest), "--output", str(output)]
        return command, [str(path) for path in evidence], [str(output)]

    def _build_md_candidate_delivery(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"pareto_run_id", "pareto_manifest_sha256"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"library-first-md-candidate-delivery missing parameters: {missing}")
        pareto_run = str(parameters["pareto_run_id"])
        pareto_spec = self.registry.load_spec(pareto_run)
        pareto_result = self.registry.load_result(pareto_run)
        if (pareto_spec.engine != "library-first-pareto"
                or pareto_result.engine != pareto_spec.engine
                or pareto_result.state != "COMPLETED" or pareto_result.return_code != 0):
            raise ValueError("A completed Library-first Pareto job is required")
        pareto_folder = self.registry.job_dir(pareto_run) / "artifacts"
        pareto_manifest = pareto_folder / "stage6_pareto_manifest.json"
        candidate_table = pareto_folder / "stage6_final_candidates.parquet"
        pareto_evidence = [self.registry.job_dir(pareto_run) / "spec.json",
            self.registry.job_dir(pareto_run) / "result.json", pareto_manifest, candidate_table]
        if any(path.is_symlink() or not path.is_file() for path in pareto_evidence):
            raise ValueError("Pareto candidate-delivery evidence is incomplete")
        if sha256_file(pareto_manifest) != str(parameters["pareto_manifest_sha256"]):
            raise ValueError("Pareto manifest checksum mismatch")
        pareto = json.loads(pareto_manifest.read_text())
        gates = pareto.get("quality_gates")
        selected = pareto.get("outputs", {}).get(candidate_table.name, {})
        if (pareto.get("status") != "completed"
                or not isinstance(gates, dict) or not gates
                or not all(value is True for value in gates.values())
                or selected.get("sha256") != sha256_file(candidate_table)):
            raise ValueError("Pareto result has not passed MD candidate-delivery admission")
        covdock_run = pareto_spec.parent_run_id
        if not isinstance(covdock_run, str):
            raise ValueError("Pareto job lacks its CovDock parent")
        covdock_spec = self.registry.load_spec(covdock_run)
        covdock_result = self.registry.load_result(covdock_run)
        if (covdock_spec.engine != "schrodinger-covdock-lo"
                or covdock_spec.route != "library-first"
                or covdock_result.engine != covdock_spec.engine
                or covdock_result.state != "COMPLETED" or covdock_result.return_code != 0):
            raise ValueError("Pareto parent is not a completed Library-first CovDock job")
        covdock_folder = self.registry.job_dir(covdock_run) / "artifacts"
        covdock_manifest = covdock_folder / "covdock_lo_registered_manifest.json"
        covdock_evidence = [self.registry.job_dir(covdock_run) / "spec.json",
            self.registry.job_dir(covdock_run) / "result.json", covdock_manifest]
        if any(path.is_symlink() or not path.is_file() for path in covdock_evidence):
            raise ValueError("CovDock candidate-delivery evidence is incomplete")
        covdock = json.loads(covdock_manifest.read_text())
        covdock_gates = covdock.get("gate_quality_gates")
        if (covdock.get("status") != "completed"
                or not isinstance(covdock_gates, dict) or not covdock_gates
                or not all(value is True for value in covdock_gates.values())):
            raise ValueError("CovDock parent has not passed candidate-delivery admission")
        from .candidate_delivery import build_candidate_results_manifest
        preview = build_candidate_results_manifest(candidate_table, route="library-first",
            task_id=run_id)
        registered_poses = {str(Path(path).resolve()) for path in covdock.get("pose_files", [])}
        asset_paths = []
        for key, record in preview["assets"].items():
            path = Path(record["path"])
            if path.is_symlink() or not path.is_file():
                raise ValueError("Candidate structure evidence is incomplete")
            if key.startswith("covdock_") and str(path.resolve()) not in registered_poses:
                raise ValueError("Candidate CovDock asset is absent from the registered run manifest")
            asset_paths.append(path)
        parameters.update(candidate_count=len(preview["candidates"]), covdock_run_id=covdock_run,
            candidate_table_sha256=sha256_file(candidate_table),
            covdock_manifest_sha256=sha256_file(covdock_manifest), raw_paths_exposed=False)
        output = self.registry.job_dir(run_id) / "artifacts/md-candidate-delivery"
        command = [self._python(), "-B", str(library_script(self.project_root,
            "freeze_md_candidate_delivery.py")), "--candidate-table", str(candidate_table),
            "--candidate-table-sha256", sha256_file(candidate_table),
            "--covdock-manifest", str(covdock_manifest),
            "--covdock-manifest-sha256", sha256_file(covdock_manifest),
            "--task-id", run_id, "--output", str(output)]
        evidence = pareto_evidence + covdock_evidence + asset_paths
        return command, [str(path) for path in evidence], [str(output)]

    def _build_md_adduct_admission(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required={"candidate_pose_artifact_id","manifest_sha256"}
        missing=sorted(required-set(parameters))
        if missing:
            raise ValueError(f"library-first-md-adduct-admission missing parameters: {missing}")
        enable_library_first()
        from covscreen.md_candidate_pose import load_candidate_pose_input
        loaded=load_candidate_pose_input(self.project_root,
            str(parameters["candidate_pose_artifact_id"]),str(parameters["manifest_sha256"]))
        runtime=self._native_adduct_runtime()
        timeout=int(parameters.get("native_timeout_seconds",1200))
        if not 60<=timeout<=3600:
            raise ValueError("native_timeout_seconds must be in [60, 3600]")
        parameters.update(candidate_id=loaded["manifest"]["candidate_id"],
            compound_id=loaded["manifest"]["compound_id"],
            reaction_chemistry=loaded["manifest"]["chemistry"],
            runtime_registration={"launcher":str(runtime["launcher"]),
                "vendor_root":str(runtime["vendor_root"]),
                "runtime_lib_dir":str(runtime["runtime_lib_dir"]),
                "license_path_registered":True,"probe":runtime["probe"]})
        # The historical executor name is not the candidate's discovery route.
        # Derive provenance only from the replayed artifact, never caller metadata.
        parameters.pop("discovery_origin", None)
        if loaded["manifest"].get("origin") is not None:
            parameters["discovery_origin"] = dict(loaded["manifest"]["origin"])
        output=self.registry.job_dir(run_id)/"artifacts/md-adduct-admission"
        command=[str(runtime["audit_python"]),"-B",str(library_script(self.project_root,
            "run_md_adduct_admission_job.py")),"--root",str(self.project_root),
            "--artifact-id",str(parameters["candidate_pose_artifact_id"]),
            "--manifest-sha256",str(parameters["manifest_sha256"]),
            "--launcher",str(runtime["launcher"]),"--vendor-root",str(runtime["vendor_root"]),
            "--license-file",str(runtime["license_file"]),
            "--runtime-lib-dir",str(runtime["runtime_lib_dir"]),"--output",str(output),
            "--timeout-seconds",str(timeout)]
        return command,[str(path) for path in loaded["evidence_files"]],[str(output)]

    def _build_covindb_prescreen(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        if parameters.get('vs_prescreen'):
            from .library_vs_prescreen_input import command
            return command(self.project_root, parameters, self._python(),
                library_script(self.project_root, 'run_covindb_prescreen.py'), self.registry.job_dir(run_id) / 'artifacts')
        required = {"target_config", "top_n"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"covindb-prescreen missing parameters: {missing}")
        source = Path(str(self.config.get("covindb", {}).get("virtual_screening_library", "")))
        if not source.is_file():
            raise ValueError("configured CovalentInDB virtual screening library is unavailable")
        target = self._within_project(str(parameters["target_config"]))
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        command = [
            self._python(),
            str(library_script(self.project_root, "run_covindb_prescreen.py")),
            "--input",
            str(source),
            "--target-config",
            str(target),
            "--output-dir",
            str(artifact_dir),
            "--top-n",
            str(int(parameters["top_n"])),
            "--chunk-size",
            str(int(parameters.get("chunk_size", 5000))),
            "--seed",
            str(int(parameters.get("seed", self.config.get("runtime", {}).get("random_seed", 20260725)))),
        ]
        if parameters.get("max_rows") is not None:
            command.extend(["--max-rows", str(int(parameters["max_rows"]))])
        selection = parameters.get("record_selection", "file_order_prefix")
        if selection not in {"file_order_prefix", "reservoir"}:
            raise ValueError("Unsupported prescreen record selection")
        if selection == "reservoir" and not parameters.get("max_rows"):
            raise ValueError("Reservoir prescreen requires bounded max_rows")
        command.extend(["--record-selection", selection])
        inputs = [str(target)]
        if parameters.get("source_sha256"):
            expected = parameters["source_sha256"]
            if sha256_file(source) != expected:
                raise ValueError("Prescreen source checksum changed; create a new plan")
            command.extend(["--input-sha256", expected])
        if parameters.get("pilot_manifest"):
            manifest = self._within_project(str(parameters["pilot_manifest"]))
            contract = json.loads(manifest.read_text())
            if (contract["source"]["sha256"] != parameters.get("source_sha256")
                    or contract["source"]["path"] != str(source)
                    or contract["limits"] != {"max_rows": parameters.get("max_rows"), "top_n": parameters["top_n"]}):
                raise ValueError("Pilot request differs from frozen input")
            if (contract.get("record_selection", "file_order_prefix") != selection
                    or contract.get("sampling_seed", 20260725) != parameters.get("seed", 20260725)):
                raise ValueError("Pilot sampling policy differs from frozen input")
            inputs.append(str(manifest))
        return command, inputs, [str(artifact_dir)]

    @staticmethod
    def _rdkit_request_payload(spec: JobSpec) -> dict:
        return {
            "request_id": spec.request_id,
            "scaffold_smiles": spec.parameters["scaffold_smiles"],
            "attachment_atom_indices": spec.parameters.get(
                "attachment_atom_indices", []
            ),
            "protected_atom_indices": spec.parameters.get(
                "protected_atom_indices", []
            ),
            "allowed_warheads": spec.parameters["allowed_warheads"],
            "allowed_linkers": spec.parameters.get(
                "allowed_linkers", ["direct"]
            ),
            "max_candidates": min(
                int(spec.parameters.get("max_candidates", spec.resource_budget.max_candidates)),
                spec.resource_budget.max_candidates,
            ),
        }

    def _build_rdkit(self, run_id: str, parameters: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
        required = {"scaffold_smiles", "allowed_warheads"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"rdkit-warhead-enumeration missing parameters: {missing}")
        job_dir = self.registry.job_dir(run_id)
        request_path = job_dir / "request.json"
        output_json = job_dir / "artifacts" / "candidates.json"
        output_csv = job_dir / "artifacts" / "candidates.csv"
        output_sdf = job_dir / "artifacts" / "candidates.sdf"
        output_table = job_dir / "artifacts" / "candidate_table.parquet"
        command = [
            self._python(),
            str(science_script("run_rdkit_enumeration.py")),
            "--request", str(request_path),
            "--output-json", str(output_json),
            "--output-csv", str(output_csv),
            "--output-sdf", str(output_sdf),
            "--output-table", str(output_table),
        ]
        return command, [str(request_path)], [
            str(output_json),
            str(output_csv),
            str(output_sdf),
            str(output_table),
        ]

    def _build_ligand_first_covalentization(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"campaign"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(
                f"ligand-first-covalentization missing parameters: {missing}"
            )
        campaign = self._within_project(str(parameters["campaign"]))
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        if parameters.get("enumeration_backend") == "upstream-native":
            repo = self._within_project(str(parameters.get("upstream_repo", "external/Covalentizer")))
            sources = [repo / "Code/CovUtils.py", repo / "Code/numbered_reaction.re",
                       repo / "Scripts/reform_elec.re"]
            if not all(path.is_file() for path in sources):
                raise ValueError("native Covalentizer source and reaction files are required")
            command = [self._python(), str(science_script("run_upstream_ligand_campaign.py")),
                       "--campaign", str(campaign), "--upstream-repo", str(repo),
                       "--output-dir", str(artifact_dir),
                       "--max-candidates", str(parameters["candidate_budget"])]
            return command, [str(campaign), *map(str, sources)], [str(artifact_dir)]
        command = [
            self._python(),
            str(science_script("generate_real_ligand_first_candidates.py")),
            "--campaign",
            str(campaign),
            "--candidate-json",
            str(artifact_dir / "ligand_first_candidates.json"),
            "--candidate-sdf",
            str(artifact_dir / "ligand_first_candidates.sdf"),
            "--ligand-table",
            str(artifact_dir / "ligand_first_candidates.parquet"),
            "--failure-json",
            str(artifact_dir / "ligand_first_failures.json"),
            "--auto-discover-sites",
            "--site-discovery-json",
            str(artifact_dir / "ligand_first_site_discovery.json"),
            "--max-sites",
            str(int(parameters.get("max_sites", 8))),
            "--max-branch-heavy-atoms",
            str(int(parameters.get("max_branch_heavy_atoms", 12))),
            "--max-distance-angstrom",
            str(float(parameters.get("max_distance_angstrom", 12.0))),
            "--max-vector-angle-degrees",
            str(float(parameters.get("max_vector_angle_degrees", 100.0))),
        ]
        return command, [str(campaign)], [str(artifact_dir)]

    def _build_ligand_first_structure_intake(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        if not parameters.get("campaign"):
            raise ValueError("ligand-first-structure-intake requires campaign")
        campaign = self._within_project(str(parameters["campaign"]))
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        command = [
            self._python(),
            str(science_script("prepare_ligand_first_inputs.py")),
            "--campaign",
            str(campaign),
            "--audit-output",
            str(artifact_dir / "ligand_first_input_audit.json"),
            "--parent-pose-output",
            str(artifact_dir / "parent_ligand_pose.sdf"),
        ]
        if parameters.get("restore_missing", True):
            command.append("--restore-missing")
        inputs = [str(campaign)]
        try:
            source_assets = json.loads(campaign.read_text()).get("source_assets", [])
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid Ligand-first campaign JSON: {exc}") from exc
        for asset in source_assets:
            path = campaign.parent / str(asset.get("path", ""))
            if path.is_file():
                inputs.append(str(path))
        return command, inputs, [str(artifact_dir)]

    def _build_carsidock(self, run_id: str, parameters: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
        required = {"pdb", "reference_ligand", "ligand_sdf", "ligand_table", "covres"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"carsidock-cov missing parameters: {missing}")
        pdb = self._within_project(str(parameters["pdb"]))
        reference = self._within_project(str(parameters["reference_ligand"]))
        ligand_sdf = self._within_project(str(parameters["ligand_sdf"]))
        ligand_table = self._within_project(str(parameters["ligand_table"]))
        carsidock_config = self.config.get("carsidock", {})
        repo = Path(carsidock_config.get("repo", ""))
        checkpoint = Path(carsidock_config.get("checkpoint", ""))
        reuse_prefix_value = parameters.get("reuse_output_prefix")
        reuse_grouped = (self._within_project(str(parameters["reuse_grouped_output_dir"]))
                         if parameters.get("reuse_grouped_output_dir") else None)
        if reuse_grouped and (reuse_prefix_value or not parameters.get("reaction_type_column")):
            raise ValueError("Grouped CarsiDock reuse requires reaction routing and excludes legacy prefix")
        reuse_prefix = (
            self._within_project(str(reuse_prefix_value), must_exist=False)
            if reuse_prefix_value
            else None
        )
        if reuse_prefix is None and (not repo.is_dir() or not checkpoint.is_file()):
            raise ValueError("configured CarsiDock repo or checkpoint is unavailable")
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        output_scores = artifact_dir / "carsidock_scores.parquet"
        command = [
            self._python(), str(science_script("run_carsidock_registered.py")),
            "--chunk", str(int(parameters.get("chunk", 0))),
            "--device", str(parameters.get("device", "cuda")),
            "--pdb", str(pdb),
            "--reference-ligand", str(reference),
            "--ligand-sdf", str(ligand_sdf),
            "--ligand-table", str(ligand_table),
            "--covres", str(parameters["covres"]),
            "--output-dir", str(artifact_dir),
            "--output-scores", str(output_scores),
            "--carsidock-repo", str(repo),
            "--checkpoint", str(checkpoint),
            "--num-conformer", str(int(parameters.get("num_conformer", 1))),
            "--cuda-device-index", str(int(parameters.get("cuda_device_index", 0))),
            "--seed", str(int(parameters.get("seed", self.config.get("runtime", {}).get("random_seed", 20260725)))),
        ]
        if parameters.get("reaction_type_column"):
            command.extend(
                ["--reaction-type-column", str(parameters["reaction_type_column"])]
            )
        elif parameters.get("reaction_type"):
            command.extend(["--reaction-type", str(parameters["reaction_type"])])
        inputs = [str(pdb), str(reference), str(ligand_sdf), str(ligand_table)]
        if parameters.get("docking_binding"):
            inputs.append(str(self._within_project(str(parameters["docking_binding"]))))
        if reuse_grouped:
            command.extend(["--reuse-grouped-output-dir", str(reuse_grouped)])
            retained = sorted(reuse_grouped.glob("carsidock_*"))
            inputs.extend(str(path) for path in retained if path.is_file())
        if reuse_prefix is not None:
            retained = [
                reuse_prefix.with_suffix(".sdf"),
                Path(f"{reuse_prefix}_nodumat.sdf"),
                Path(f"{reuse_prefix}_withpckt.pdb"),
            ]
            missing_retained = [str(path) for path in retained if not path.is_file()]
            if missing_retained:
                raise ValueError(
                    f"reusable CarsiDock outputs are incomplete: {missing_retained}"
                )
            command.extend(["--reuse-output-prefix", str(reuse_prefix)])
            inputs.extend(str(path) for path in retained)
        return command, inputs, [str(artifact_dir)]

    def _build_rtmscore(self, run_id: str, parameters: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
        required = {"pdb", "reference_ligand", "carsidock_scores"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"rtmscore missing parameters: {missing}")
        pdb = self._within_project(str(parameters["pdb"]))
        reference = self._within_project(str(parameters["reference_ligand"]))
        carsidock_scores = self._within_project(str(parameters["carsidock_scores"]))
        carsidock_config = self.config.get("carsidock", {})
        repo = Path(carsidock_config.get("repo", ""))
        checkpoint = Path(carsidock_config.get("rtmscore_checkpoint", ""))
        if not repo.is_dir() or not checkpoint.is_file():
            raise ValueError("configured CarsiDock repo or RTMScore checkpoint is unavailable")
        output_csv = self.registry.job_dir(run_id) / "artifacts" / "rtmscore.csv"
        command = [
            self._python(), str(science_script("run_rtmscore_existing_poses.py")),
            "--project-root", str(self.project_root),
            "--carsidock-repo", str(repo),
            "--checkpoint", str(checkpoint),
            "--pdb", str(pdb),
            "--reference-ligand", str(reference),
            "--carsidock-scores", str(carsidock_scores),
            "--output", str(output_csv),
            "--cuda-device-index", str(int(parameters.get("cuda_device_index", 0))),
        ]
        if parameters.get("max_ligands") is not None:
            command.extend(["--max-ligands", str(int(parameters["max_ligands"]))])
        return command, [str(pdb), str(reference), str(carsidock_scores)], [str(output_csv), str(output_csv.with_suffix(".parquet"))]

    def _build_recall_pool(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"carsidock_scores", "top_n"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"carsidock-pool-selection missing parameters: {missing}")
        carsidock_scores = self._within_project(str(parameters["carsidock_scores"]))
        rtmscore_scores = (
            self._within_project(str(parameters["rtmscore_scores"]))
            if parameters.get("rtmscore_scores")
            else None
        )
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        command = [
            self._python(),
            str(science_script("select_carsidock_pool.py")),
            "--carsidock-scores",
            str(carsidock_scores),
            "--output-dir",
            str(artifact_dir),
            "--top-n",
            str(int(parameters["top_n"])),
            "--min-carsidock-success-rate",
            str(float(parameters.get("min_carsidock_success_rate", 0.75))),
        ]
        if rtmscore_scores is not None:
            command.extend(["--rtmscore-scores", str(rtmscore_scores)])
        for key, flag in (
            ("score_union_n", "--score-union-n"),
            ("diversity_n", "--diversity-n"),
            ("rescue_n", "--rescue-n"),
        ):
            if parameters.get(key) is not None:
                command.extend([flag, str(int(parameters[key]))])
        inputs = [str(carsidock_scores)]
        if rtmscore_scores is not None:
            inputs.append(str(rtmscore_scores))
        return command, inputs, [str(artifact_dir)]

    def _build_af3_batch(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"candidate_table", "source_data_json", "target_config", "top_n"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"alphafold3-mpae missing parameters: {missing}")
        candidate_table = self._within_project(str(parameters["candidate_table"]))
        from .refinement_input import refinement_input_arguments
        contract_arguments = refinement_input_arguments(candidate_table, parameters)
        source_data = self._within_project(str(parameters["source_data_json"]))
        target_config = self._within_project(str(parameters["target_config"]))
        target = yaml.safe_load(target_config.read_text()) or {}
        target_required = {
            "target_id",
            "protein_chain",
            "cysteine_sequence_index",
            "residue_number",
            "nucleophilic_atom",
        }
        target_missing = sorted(target_required - set(target))
        if target_missing:
            raise ValueError(f"AF3 target config is missing fields: {target_missing}")
        from .af3_target import read_target_seed
        seed_check = read_target_seed(source_data, target_config,
            overrides={**target, "ligand_chain": target.get("ligand_chain", "L")})
        parameters["af3_target_seed_check"] = seed_check
        from .af3_target import runtime_paths
        assets = runtime_paths(self.config.get("alphafold3", {}))
        runner, python, model_dir, database_dir = (assets[key] for key in
            ("runner", "python", "model_dir", "database_dir"))
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        command = [
            self._python(),
            str(science_script("run_af3_registered_batch.py")),
            "--candidate-table",
            str(candidate_table),
            "--source-data-json",
            str(source_data),
            "--target-config",
            str(target_config),
            "--output-dir",
            str(artifact_dir),
            "--runner",
            str(runner),
            "--af3-python",
            str(python),
            "--model-dir",
            str(model_dir),
            "--database-dir",
            str(database_dir),
            "--protein-chain",
            str(target["protein_chain"]),
            "--ligand-chain",
            str(seed_check["ligand_chain"]),
            "--expected-source-data-sha256",
            str(seed_check["source_data_sha256"]),
            "--expected-target-config-sha256",
            str(seed_check["target_config_sha256"]),
            "--cysteine-sequence-index",
            str(int(target["cysteine_sequence_index"])),
            "--target-id",
            str(target["target_id"]),
            "--source-residue-number",
            str(int(target["residue_number"])),
            "--nucleophilic-atom",
            str(target["nucleophilic_atom"]),
            "--top-n",
            str(int(parameters["top_n"])),
            "--gpu",
            str(int(parameters.get("cuda_device_index", 0))),
            "--num-recycles",
            str(int(parameters.get("num_recycles", 3))),
            "--num-diffusion-samples",
            str(int(parameters.get("num_diffusion_samples", 1))),
            "--num-seeds",
            str(int(parameters.get("num_seeds", 1))),
            "--cpu-per-search",
            str(int(parameters.get("cpu_per_search", 2))),
            "--random-seed",
            str(int(parameters.get("seed", self.config.get("runtime", {}).get("random_seed", 20260725)))),
            "--min-coverage",
            str(float(parameters.get("min_coverage", 0.95))),
        ]
        if parameters.get("resume", True):
            command.append("--resume")
        if parameters.get("run_data_pipeline", False):
            command.append("--run-data-pipeline")
        command.extend(contract_arguments)
        inputs = [str(candidate_table), str(source_data), str(target_config)]
        inputs.extend(self._refinement_lineage(parameters))
        if parameters.get("reuse_artifact_dir"):
            reuse_artifact_dir = self._within_project(
                str(parameters["reuse_artifact_dir"])
            )
            command.extend(["--reuse-artifact-dir", str(reuse_artifact_dir)])
            for relative in (
                "prepared/batch_manifest.json",
                "stage4_selection_manifest.json",
            ):
                source = reuse_artifact_dir / relative
                if source.is_file():
                    inputs.append(str(source))
        return command, inputs, [str(artifact_dir)]

    def _refinement_lineage(self, parameters):
        if 'refinement_lineage_files' not in parameters:
            return []
        lineage = parameters['refinement_lineage_files']
        if (not isinstance(lineage, list) or not 1 <= len(lineage) <= 256
                or any(not isinstance(value, str) or not value for value in lineage)
                or len(set(lineage)) != len(lineage)):
            raise ValueError('Refinement lineage requires distinct bound input files (maximum 256)')
        paths = [self._within_project(value) for value in lineage]
        if any(not path.is_file() for path in paths):
            raise ValueError('Refinement lineage entries must be files')
        return [str(path) for path in paths]

    def _build_covdock_vs(self, run_id, parameters):
        from .covdock_vs_preparation import revalidate
        if set(parameters) != {'vs_preparation'}:
            raise ValueError('VS accepts only its frozen preparation plan')
        packet = revalidate(self.project_root, parameters['vs_preparation'])
        configured = self.config.get('schrodinger', {})
        from .covdock_receptor import runtime_paths
        runtime = runtime_paths(configured)
        if {name: str(path.resolve()) if path else None for name, path in runtime.items()} != packet['runtime']:
            raise ValueError('VS runtime differs from the configured installation')
        parameters['input_row_count'] = packet['candidate_count']
        command = [sys.executable, str(science_script('run_covdock_vs.py')),
                   '--project-root', str(self.project_root), '--run-id', run_id]
        return command, [str(self.project_root/name) for name in packet['input_sha256']], [
            str(self.registry.job_dir(run_id)/'artifacts')]

    def _build_covdock_lo(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {
            "candidate_table",
            "receptor",
            "attachment_residue",
            "final_n",
        }
        if not parameters.get("reaction_type_column"):
            required.add("reaction_type")
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"schrodinger-covdock-lo missing parameters: {missing}")
        candidate_table = self._within_project(str(parameters["candidate_table"]))
        from .refinement_input import refinement_input_arguments
        from .covdock_gate import ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION, LIGAND_ALGORITHM_VERSION
        contract_arguments = refinement_input_arguments(candidate_table, parameters)
        score_source = parameters.get("input_score_source")
        if ("input_score_source" in parameters or contract_arguments) and score_source not in {"af3", "core"}:
            raise ValueError("Exact-input CovDock requires explicit input_score_source: af3 or core")
        gate = parameters.get("gate_algorithm_version")
        if "gate_algorithm_version" in parameters and gate not in {ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION, LIGAND_ALGORITHM_VERSION}:
            raise ValueError("Unsupported CovDock gate_algorithm_version")
        if parameters.get("reaction_type_column") and (contract_arguments or score_source is not None or gate is not None):
            raise ValueError("Grouped CovDock does not yet implement the explicit refinement input/source/policy contract")
        receptor = self._within_project(str(parameters["receptor"]))
        from .covdock_receptor import inspect_receptor
        receptor_check = inspect_receptor(receptor, parameters["attachment_residue"],
            parameters.get("nucleophilic_atom", "SG"))
        parameters["covdock_receptor_input_check"] = receptor_check
        grid_file = (
            self._within_project(str(parameters["grid_file"]))
            if parameters.get("grid_file")
            else None
        )
        from .covdock_receptor import runtime_paths
        runtime = runtime_paths(self.config.get('schrodinger', {}))
        root, license_file, runtime_lib = runtime['root'], runtime['license_file'], runtime['runtime_lib_dir']
        if parameters.get('workflow_id'):
            from .covdock_readback import freeze
            parameters['covdock_readback'] = freeze(self.config.get('schrodinger', {}), parameters['attachment_residue'])
            if parameters.get('denovo_source_id'):
                from covscreen_de_novo.control_review import freeze as freeze_control
                parameters['denovo_control_readback'] = freeze_control()
                from covscreen_de_novo.chemical_review import freeze as freeze_chemical
                parameters['denovo_chemical_readback'] = freeze_chemical()
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        grouped_reactions = bool(parameters.get("reaction_type_column"))
        runner = (
            "run_ligand_first_covdock_grouped.py"
            if grouped_reactions
            else "run_covdock_lo_registered.py"
        )
        command = [
            self._python(),
            str(science_script(runner)),
            "--candidate-table", str(candidate_table),
            "--receptor", str(receptor),
            "--attachment-residue", str(parameters["attachment_residue"]),
            "--nucleophilic-atom", str(parameters.get("nucleophilic_atom", "SG")),
            "--output-dir", str(artifact_dir),
            "--schrodinger-root", str(root),
            "--license-file", str(license_file),
            "--njobs", str(int(parameters.get("njobs", 1))),
            "--final-n", str(int(parameters["final_n"])),
            "--max-local-strain", str(float(parameters.get("max_local_strain", 10.0))),
            "--hard-max-strain", str(float(parameters.get("hard_max_strain", 20.0))),
            "--min-covalent-bond-length", str(
                float(parameters.get("min_covalent_bond_length", 1.5))
            ),
            "--max-covalent-bond-length", str(
                float(parameters.get("max_covalent_bond_length", 2.2))
            ),
            "--max-pose-rmsd", str(float(parameters.get("max_pose_rmsd", 3.0))),
            "--severe-clash-distance", str(
                float(parameters.get("severe_clash_distance", 1.2))
            ),
            "--min-success-rate", str(float(parameters.get("min_success_rate", 0.80))),
        ]
        if grouped_reactions:
            command.extend(
                ["--reaction-type-column", str(parameters["reaction_type_column"])]
            )
            checkpoint_size = int(parameters.get("checkpoint_size", 0))
            if checkpoint_size:
                command.extend(["--checkpoint-size", str(checkpoint_size)])
                command.extend(
                    [
                        "--checkpoint-workers",
                        str(int(parameters.get("checkpoint_workers", 1))),
                        "--checkpoint-worker-cpus",
                        str(int(parameters.get("checkpoint_worker_cpus", 1))),
                    ]
                )
        else:
            command.extend(["--reaction-type", str(parameters["reaction_type"])])
            command.extend(["--expected-receptor-sha256", receptor_check["receptor_sha256"]])
        if runtime_lib is not None:
            command.extend(["--runtime-lib-dir", str(runtime_lib)])
        if parameters.get("prepare_receptor", False):
            command.append("--prepare-receptor")
        if grid_file is not None:
            command.extend(["--grid-file", str(grid_file)])
            if not grouped_reactions:
                command.extend(["--expected-grid-sha256", sha256_file(grid_file)])
        if parameters.get("host") and not grouped_reactions:
            command.extend(["--host", str(parameters["host"])])
        if parameters.get("min_radius") is not None:
            command.extend(["--min-radius", str(float(parameters["min_radius"]))])
        if parameters.get("score_property"):
            command.extend(["--score-property", str(parameters["score_property"])])
        reuse_artifact_dir = (
            self._within_project(str(parameters["reuse_artifact_dir"]))
            if parameters.get("reuse_artifact_dir")
            else None
        )
        if reuse_artifact_dir is not None:
            command.extend(["--reuse-artifact-dir", str(reuse_artifact_dir)])
        pose_consistency_table = (
            self._within_project(str(parameters["pose_consistency_table"]))
            if parameters.get("pose_consistency_table")
            else None
        )
        if pose_consistency_table is not None:
            command.extend(
                ["--pose-consistency-table", str(pose_consistency_table)]
            )
        if parameters.get("resume", True):
            command.append("--resume")
        command.extend(contract_arguments)
        if score_source is not None:
            command.extend(["--input-score-source", score_source])
        if gate is not None:
            command.extend(["--gate-algorithm-version", gate])
        inputs = [str(candidate_table), str(receptor)]
        inputs.extend(self._refinement_lineage(parameters))
        if parameters.get('retained_candidates'):
            from .covdock_retained import validate_retained_recovery
            retained = validate_retained_recovery(
                self.project_root, parameters, parameters['retained_candidates'])
            command.extend(['--project-root', str(self.project_root)])
            for verified in retained.values():
                command.extend(['--retained-candidate', str(verified['descriptor_path']),
                                verified['descriptor_sha256']])
                inputs.append(str(verified['descriptor_path']))
                inputs.extend(str(path) for path in verified['files'].values())
        if reuse_artifact_dir is not None:
            inputs.append(str(reuse_artifact_dir))
        if pose_consistency_table is not None:
            inputs.append(str(pose_consistency_table))
        if grid_file is not None:
            inputs.append(str(grid_file))
        return command, inputs, [str(artifact_dir)]

    def _build_library_first_pareto(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"candidate_table", "final_n"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"library-first-pareto missing parameters: {missing}")
        candidate_table = self._within_project(str(parameters["candidate_table"]))
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        command = [
            self._python(),
            str(Path(__file__).resolve().parents[2] / "scripts/select_library_first_pareto.py"),
            "--candidate-table", str(candidate_table),
            "--output-dir", str(artifact_dir),
            "--final-n", str(int(parameters["final_n"])),
        ]
        inputs = [str(candidate_table)]
        for column, direction in parameters.get("additional_objectives", {}).items():
            command.extend(["--objective", f"{column}:{direction}"])
        if parameters.get("_pareto_profile"):
            command.extend(["--profile", str(parameters["_pareto_profile"])])
        if parameters.get("_pareto_profile") == "ligand-first":
            trace_parent = str(parameters.get("trace_parent_run_id", ""))
            if not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{5,127}", trace_parent
            ):
                raise ValueError(
                    "Ligand-first Pareto requires a safe trace_parent_run_id"
                )
            trace_config = self._within_project(
                str(parameters.get("trace_chain_config", ""))
            )
            command.extend(
                [
                    "--trace-project-root",
                    str(self.project_root),
                    "--trace-parent-run-id",
                    trace_parent,
                    "--trace-current-run-id",
                    run_id,
                    "--trace-chain-config",
                    str(trace_config),
                ]
            )
            inputs.append(str(trace_config))
        return command, inputs, [str(artifact_dir)]

    def _build_ligand_first_evaluation(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"candidate_table", "campaign"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"ligand-first-evaluation missing parameters: {missing}")
        candidate_table = self._within_project(str(parameters["candidate_table"]))
        campaign = self._within_project(str(parameters["campaign"]))
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        command = [
            self._python(),
            str(science_script("evaluate_ligand_first_batch.py")),
            "--candidate-table",
            str(candidate_table),
            "--campaign",
            str(campaign),
            "--output-dir",
            str(artifact_dir),
            "--min-parent-mcs-fraction",
            str(float(parameters.get("min_parent_mcs_fraction", 0.60))),
        ]
        if parameters.get("recheck_coordinates"):
            schrodinger = self.config.get("schrodinger", {})
            root = Path(str(schrodinger.get("root", "")))
            if not schrodinger.get("enabled") or not (root / "run").is_file():
                raise ValueError("Coordinate recheck requires configured Schrödinger runtime")
            residue = str(parameters.get("attachment_residue", ""))
            match = re.fullmatch(r"([^:\s]+):(\d+)", residue)
            if not match:
                raise ValueError("Coordinate recheck requires attachment_residue CHAIN:NUMBER")
            command.extend(["--schrodinger-root", str(root),
                            "--attachment-chain", match[1],
                            "--attachment-residue", match[2]])
        return command, [str(candidate_table), str(campaign)], [str(artifact_dir)]

    def _build_library_first_evaluation(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"score_table", "engine_name", "score_direction"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"library-first-evaluation missing parameters: {missing}")
        score_table = self._within_project(str(parameters["score_table"]))
        if parameters["score_direction"] not in {"higher_is_better", "lower_is_better"}:
            raise ValueError("score_direction must be higher_is_better or lower_is_better")
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        command = [
            self._python(),
            str(library_script(self.project_root, "run_library_first_evaluation.py")),
            "--score-table", str(score_table),
            "--engine-name", str(parameters["engine_name"]),
            "--score-direction", str(parameters["score_direction"]),
            "--output-dir", str(artifact_dir),
        ]
        for key, flag in (
            ("expected_rows", "--expected-rows"),
            ("expected_active", "--expected-active"),
            ("expected_decoy", "--expected-decoy"),
        ):
            if parameters.get(key) is not None:
                command.extend([flag, str(int(parameters[key]))])
        return command, [str(score_table)], [str(artifact_dir)]

    def _generator_assets(self, name: str) -> tuple[Path, Path, Path]:
        configured = self.config.get("de_novo", {}).get(name, {})
        repo = Path(configured.get("repo", ""))
        python = Path(configured.get("python", ""))
        checkpoint = Path(configured.get("checkpoint", ""))
        if not configured.get("enabled", False):
            raise ValueError(f"configured generator is disabled: {name}")
        if not repo.is_dir() or not python.is_file() or not checkpoint.is_file():
            raise ValueError(f"configured {name} repo, python, or checkpoint is unavailable")
        return repo, python, checkpoint

    def _build_pocketxmol(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"protein_pdb", "pocket_center", "n_candidates"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"pocketxmol missing parameters: {missing}")
        protein = self._within_project(str(parameters["protein_pdb"]))
        repo, python, checkpoint = self._generator_assets("pocketxmol")
        if not (repo / "scripts" / "sample_use.py").is_file():
            raise ValueError("PocketXMol sampling entry point is unavailable")
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        center = [float(value) for value in parameters["pocket_center"]]
        command = [
            self._python(),
            str(science_script("run_pocketxmol_registered.py")),
            "--repo", str(repo),
            "--generator-python", str(python),
            "--checkpoint", str(checkpoint),
            "--protein", str(protein),
            "--pocket-center", *(str(value) for value in center),
            "--pocket-radius", str(float(parameters.get("pocket_radius", 15.0))),
            "--mean-atoms", str(int(parameters.get("mean_atoms", 28))),
            "--std-atoms", str(float(parameters.get("std_atoms", 2.0))),
            "--output-dir", str(artifact_dir),
            "--num-molecules", str(int(parameters["n_candidates"])),
            "--seed", str(int(parameters["seed"])),
            "--cuda-device-index", str(int(parameters.get("cuda_device_index", 0))),
            "--device", str(parameters["device"]),
        ]
        return command, [str(protein)], [str(artifact_dir)]

    def _build_reinvent4(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"n_candidates"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"reinvent4 missing parameters: {missing}")
        repo, python, checkpoint = self._generator_assets("reinvent4")
        if not (python.parent / "reinvent").is_file():
            raise ValueError("REINVENT4 executable is unavailable beside configured python")
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        command = [
            self._python(),
            str(science_script("run_reinvent4_registered.py")),
            "--repo", str(repo),
            "--generator-python", str(python),
            "--checkpoint", str(checkpoint),
            "--output-dir", str(artifact_dir),
            "--num-smiles", str(int(parameters["n_candidates"])),
            "--seed", str(int(parameters["seed"])),
            "--cuda-device-index", str(int(parameters.get("cuda_device_index", 0))),
            "--device", str(parameters["device"]),
        ]
        return command, [], [str(artifact_dir)]

    def _build_difflinker(
        self, run_id: str, parameters: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        required = {"protein_pdb", "fragments_sdf", "linker_size", "n_candidates"}
        missing = sorted(required - set(parameters))
        if missing:
            raise ValueError(f"difflinker missing parameters: {missing}")
        protein = self._within_project(str(parameters["protein_pdb"]))
        fragments = self._within_project(str(parameters["fragments_sdf"]))
        repo, python, checkpoint = self._generator_assets("difflinker")
        if not (repo / "generate_with_protein.py").is_file():
            raise ValueError("DiffLinker generation entry point is unavailable")
        artifact_dir = self.registry.job_dir(run_id) / "artifacts"
        command = [
            self._python(),
            str(science_script("run_difflinker_registered.py")),
            "--repo", str(repo),
            "--generator-python", str(python),
            "--checkpoint", str(checkpoint),
            "--protein", str(protein),
            "--fragments", str(fragments),
            "--output-dir", str(artifact_dir),
            "--n-samples", str(int(parameters["n_candidates"])),
            "--linker-size", str(parameters["linker_size"]),
            "--seed", str(int(parameters["seed"])),
            "--cuda-device-index", str(int(parameters.get("cuda_device_index", 0))),
            "--device", str(parameters["device"]),
        ]
        if parameters.get("n_steps") is not None:
            command.extend(["--n-steps", str(int(parameters["n_steps"]))])
        if parameters.get("anchors"):
            command.extend(["--anchors", str(parameters["anchors"])])
        return command, [str(protein), str(fragments)], [str(artifact_dir)]

    def _build_de_novo_prereaction(self, run_id, parameters):
        from .layout import enable_de_novo
        enable_de_novo()
        from covscreen_de_novo.prereaction_workflow import build
        return build(self, run_id, parameters)

    def _build_de_novo_glide(self, run_id, parameters):
        from .layout import enable_de_novo
        enable_de_novo()
        from covscreen_de_novo.batch_glide import build
        return build(self, run_id, parameters)

    def build_spec(self, request: JobSubmissionRequest) -> JobSpec:
        from .docking_policy import require_engine_enabled
        require_engine_enabled(request.engine, route=request.route)
        from .stage_boundary import require_prescreen_parent
        require_prescreen_parent(self.project_root, request)
        run_id = _safe_run_id(request.engine)
        builders = {
            "shared-deepcosi-site-prediction": self._build_deepcosi_site_prediction,
            "library-first-receptor-preparation": self._build_receptor_preparation,
            "library-first-adduct-parameterization": self._build_adduct_parameterization,
            "library-first-openmm-system-preparation": self._build_openmm_system_preparation,
            "library-first-openmm-short-qc": self._build_openmm_short_qc,
            "library-first-openmm-equilibration": self._build_openmm_equilibration,
            "library-first-openmm-production": self._build_openmm_production,
            "library-first-trajectory-audit": self._build_trajectory_audit,
            "library-first-structural-analysis": self._build_structural_analysis,
            "library-first-md-report": self._build_md_report,
            "library-first-md-candidate-delivery": self._build_md_candidate_delivery,
            "library-first-md-adduct-admission": self._build_md_adduct_admission,
            "covindb-prescreen": self._build_covindb_prescreen,
            "rdkit-warhead-enumeration": self._build_rdkit,
            "ligand-first-structure-intake": self._build_ligand_first_structure_intake,
            "ligand-first-covalentization": self._build_ligand_first_covalentization,
            "carsidock-cov": self._build_carsidock,
            "rtmscore": self._build_rtmscore,
            "carsidock-pool-selection": self._build_recall_pool,
            "alphafold3-mpae": self._build_af3_batch,
            "schrodinger-covdock-lo": self._build_covdock_lo,
            "schrodinger-covdock-vs": self._build_covdock_vs,
            "de-novo-prereaction-vina": self._build_de_novo_prereaction,
            "de-novo-prereaction-glide": self._build_de_novo_glide,
            "ligand-first-evaluation": self._build_ligand_first_evaluation,
            "ligand-first-pareto": self._build_library_first_pareto,
            "library-first-pareto": self._build_library_first_pareto,
            "library-first-evaluation": self._build_library_first_evaluation,
            "pocketxmol": self._build_pocketxmol,
            "reinvent4": self._build_reinvent4,
            "difflinker": self._build_difflinker,
        }
        if request.engine not in builders:
            raise ValueError(f"engine is registered for schema compatibility but has no execution adapter yet: {request.engine}")
        if request.engine == 'de-novo-prereaction-glide':
            from .layout import enable_de_novo
            enable_de_novo()
            from covscreen_de_novo.batch_glide import validate_request
            validate_request(self.project_root, request)
        if request.engine == 'de-novo-prereaction-vina':
            from .layout import enable_de_novo
            enable_de_novo()
            from covscreen_de_novo.prereaction_workflow import validate_request
            validate_request(self.project_root, request)
        library_only = {
            "library-first-adduct-parameterization",
            "library-first-openmm-system-preparation",
            "library-first-openmm-short-qc",
            "library-first-openmm-equilibration",
            "library-first-openmm-production",
            "library-first-trajectory-audit",
            "library-first-structural-analysis",
            "library-first-md-report",
            "library-first-md-candidate-delivery",
            "library-first-md-adduct-admission",
            "covindb-prescreen",
        }
        if request.engine == "shared-deepcosi-site-prediction" and request.route != "shared":
            raise ValueError("DeepCoSI site prediction is registered only as a shared upstream job")
        if request.engine in library_only and request.route != "library-first":
            raise ValueError(f"{request.engine} is registered only for the library-first route")
        if request.engine == "carsidock-pool-selection" and request.route not in {
            "library-first",
            "ligand-first",
        }:
            raise ValueError(
                "carsidock-pool-selection is registered only for Library-first or Ligand-first"
            )
        if request.engine == "schrodinger-covdock-lo" and request.route not in {
            "library-first",
            "ligand-first",
            "de-novo",
        }:
            raise ValueError(
                f"{request.engine} requires a candidate discovery route"
            )
        if request.engine == "schrodinger-covdock-lo" and request.route == "de-novo":
            from .refinement_input import CONTRACT_PARAMETERS, verify_refinement_input
            verify_refinement_input(self._within_project(str(request.parameters.get("candidate_table", ""))),
                *(request.parameters.get(key) for key in CONTRACT_PARAMETERS), expected_route="de-novo")
            if request.parameters.get('workflow_id'):
                from .layout import enable_de_novo
                enable_de_novo()
                if request.parameters.get('denovo_batch_lo_sha256'):
                    from covscreen_de_novo.batch_lo import validate_request, validate_evidence
                    # Execute mode validates evidence with its frozen compiled spec below.
                    if request.execution_mode != 'execute':
                        validate_evidence(self.project_root, request.parameters['denovo_batch_lo_sha256'])
                else:
                    from covscreen_de_novo.independent_review import validate_request
                validate_request(self.project_root, request)
        if (request.engine == 'schrodinger-covdock-lo' and request.route == 'ligand-first'
                and request.parameters.get('workflow_id')):
            from .ligand_covdock_execution import validate_request
            validate_request(self.project_root, request)
        if request.engine == 'schrodinger-covdock-lo' and request.route == 'library-first' and request.parameters.get('workflow_id'):
            from .library_lo_execution import validate_request
            validate_request(self.project_root, request)
        if (request.engine in {'library-first-openmm-system-preparation', 'library-first-openmm-short-qc', 'library-first-openmm-equilibration'}
                and request.parameters.get('workflow_id') and request.execution_mode == 'execute'):
            from .ligand_md_preparation import validate_request
            validate_request(self.project_root, request)
        if (request.engine == 'library-first-adduct-parameterization' and request.parameters.get('workflow_id')
                and request.execution_mode == 'execute'):
            from .ligand_md_parameterization import validate_request
            validate_request(self.project_root, request)
        if (request.engine == 'library-first-md-adduct-admission' and request.parameters.get('workflow_id')
                and request.execution_mode == 'execute'):
            from .ligand_md_graph_execution import validate_request
            validate_request(self.project_root, request)
        if request.engine in {"ligand-first-evaluation", "ligand-first-pareto"} and request.route != "ligand-first":
            raise ValueError(f"{request.engine} is registered only for the ligand-first route")
        if request.engine == "library-first-pareto" and request.route != "library-first":
            raise ValueError("library-first-pareto is registered only for the library-first route")
        if (
            request.engine == "ligand-first-covalentization"
            and request.route != "ligand-first"
        ):
            raise ValueError(
                "ligand-first-covalentization is registered only for the ligand-first route"
            )
        if (
            request.engine == "ligand-first-structure-intake"
            and request.route != "ligand-first"
        ):
            raise ValueError(
                "ligand-first-structure-intake is registered only for the ligand-first route"
            )
        gpu_engines = {"carsidock-cov", "rtmscore", "alphafold3-mpae"}
        cpu_only_carsidock_recovery = bool(
            request.engine == "carsidock-cov"
            and (request.parameters.get("reuse_output_prefix") or request.parameters.get("reuse_grouped_output_dir"))
        )
        cpu_only_af3_reparse = bool(
            request.engine == "alphafold3-mpae"
            and request.parameters.get("reuse_artifact_dir")
        )
        if (
            request.engine in gpu_engines
            and not cpu_only_carsidock_recovery
            and not cpu_only_af3_reparse
            and request.resource_budget.gpu_count < 1
        ):
            raise ValueError(f"{request.engine} requires gpu_count >= 1")
        parameters = dict(request.parameters)
        if request.engine == 'schrodinger-covdock-vs':
            from .covdock_vs_preparation import revalidate
            packet = revalidate(self.project_root, parameters['vs_preparation'])
            plan = packet['plan']
            if (request.route != plan['source']['route'] or request.project_id != plan['source']['workflow_id']
                    or request.resource_budget.model_dump(mode='json') != plan['budget']
                    or request.attempt != 1 or request.parent_run_id is not None):
                raise ValueError('VS request must retain its exact source, first-attempt identity and CPU budget')
            if request.execution_mode == 'execute':
                from .docking_policy import POLICY
                if not POLICY['native_vs_workflow_ready']:
                    raise ValueError('CovDock-VS native workflow migration is not yet complete')
                if request.route == 'ligand-first':
                    from .ligand_vs_execution import validate_request
                    validate_request(self.project_root, request)
                elif request.route == 'de-novo':
                    from .layout import enable_de_novo
                    enable_de_novo()
                    from covscreen_de_novo.batch_vs import validate_request
                    validate_request(self.project_root, request, self.config['schrodinger'])
                elif request.route == 'library-first':
                    from .library_vs_execution import validate_request
                    validate_request(self.project_root, request)
        if request.engine == "shared-deepcosi-site-prediction":
            if request.resource_budget.gpu_count != 0:
                raise ValueError("DeepCoSI site prediction is registered as CPU-only")
            if (request.resource_budget.max_runtime_minutes > 60
                    or request.resource_budget.cpu_count > 4
                    or request.resource_budget.memory_gb > 16
                    or request.resource_budget.max_candidates > 1000):
                raise ValueError("DeepCoSI site prediction exceeds its registered resource ceiling")
            timeout = int(parameters.get("native_timeout_seconds", 900))
            if not 60 <= timeout <= min(3600, request.resource_budget.max_runtime_minutes * 60):
                raise ValueError("DeepCoSI timeout exceeds the registered job budget")
            parameters["native_timeout_seconds"] = timeout
        if request.engine == "library-first-receptor-preparation":
            from .receptor_input import load_receptor_preparation_input
            receptor_input = load_receptor_preparation_input(self.project_root,
                parameters.get('receptor_input_artifact_id'), parameters.get('manifest_sha256'))
            if (request.route != receptor_input['source_route'] or
                    receptor_input.get('source_project_id', request.project_id) != request.project_id):
                raise ValueError('Receptor preparation route/project differs from its bound source')
            altlocs = parameters.get("altlocs")
            if not isinstance(altlocs, dict) or any(
                not isinstance(key, str) or not re.fullmatch(r"-?[0-9]+[A-Za-z]?", key)
                or not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]", value)
                for key, value in altlocs.items()
            ):
                raise ValueError("altlocs must map author residue identifiers to one-character labels")
            heavy_seed = int(parameters["heavy_seed"])
            max_missing = int(parameters["max_missing_residues"])
            if heavy_seed < 1 or heavy_seed > 2**31 - 1:
                raise ValueError("heavy_seed must be a positive signed 32-bit integer")
            if max_missing < 0 or max_missing > 100:
                raise ValueError("max_missing_residues must be between 0 and 100")
            if request.resource_budget.gpu_count != 0:
                raise ValueError("receptor preparation is registered as a CPU-only job")
            if request.resource_budget.max_candidates != 1:
                raise ValueError("receptor preparation requires max_candidates=1")
            if request.resource_budget.max_runtime_minutes > 60:
                raise ValueError("receptor preparation runtime budget cannot exceed 60 minutes")
            if request.resource_budget.cpu_count > 4 or request.resource_budget.memory_gb > 16:
                raise ValueError("receptor preparation exceeds the registered CPU or memory ceiling")
            parameters.update(heavy_seed=heavy_seed, max_missing_residues=max_missing,
                              altlocs=dict(sorted(altlocs.items())))
        if request.engine == "library-first-adduct-parameterization":
            if request.resource_budget.gpu_count != 0:
                raise ValueError("adduct parameterization is registered as a CPU-only job")
            if request.resource_budget.max_candidates != 1:
                raise ValueError("adduct parameterization requires max_candidates=1")
            if request.resource_budget.max_runtime_minutes > 60:
                raise ValueError("adduct parameterization runtime budget cannot exceed 60 minutes")
            if request.resource_budget.cpu_count > 4 or request.resource_budget.memory_gb > 16:
                raise ValueError("adduct parameterization exceeds the registered CPU or memory ceiling")
            timeout = int(parameters.get("native_timeout_seconds", 1200))
            if not 60 <= timeout <= min(3600, request.resource_budget.max_runtime_minutes * 60):
                raise ValueError("native parameterization timeout exceeds its registered job budget")
            parameters["native_timeout_seconds"] = timeout
        if request.engine == "library-first-openmm-system-preparation":
            if request.resource_budget.gpu_count != 0:
                raise ValueError("OpenMM system preparation is registered as a CPU-only job")
            if request.resource_budget.max_candidates != 1:
                raise ValueError("OpenMM system preparation requires max_candidates=1")
            if request.resource_budget.max_runtime_minutes > 60:
                raise ValueError("OpenMM system preparation runtime budget cannot exceed 60 minutes")
            if request.resource_budget.cpu_count > 4 or request.resource_budget.memory_gb > 16:
                raise ValueError("OpenMM system preparation exceeds the registered CPU or memory ceiling")
            parameters["cpu_threads"] = request.resource_budget.cpu_count
            iterations = int(parameters.get("max_minimization_iterations", 100))
            if not 1 <= iterations <= 1000:
                raise ValueError("max_minimization_iterations must be in [1, 1000]")
            parameters["max_minimization_iterations"] = iterations
        if request.engine == "library-first-openmm-short-qc":
            if request.resource_budget.gpu_count != 0:
                raise ValueError("OpenMM short-QC is registered as a CPU-only job")
            if request.resource_budget.max_candidates != 1:
                raise ValueError("OpenMM short-QC requires max_candidates=1")
            if request.resource_budget.max_runtime_minutes > 60:
                raise ValueError("OpenMM short-QC runtime budget cannot exceed 60 minutes")
            if request.resource_budget.cpu_count > 4 or request.resource_budget.memory_gb > 16:
                raise ValueError("OpenMM short-QC exceeds the registered CPU or memory ceiling")
            parameters["cpu_threads"] = request.resource_budget.cpu_count
            steps = int(parameters.get("steps", 1000))
            interval = int(parameters.get("report_interval", 100))
            seed = int(parameters.get("velocity_seed", 20261014))
            if not 10 <= steps <= 10000 or not 1 <= interval <= steps or steps % interval:
                raise ValueError("OpenMM short-QC steps must be divisible by report_interval")
            if not 1 <= seed <= 2147483647:
                raise ValueError("OpenMM short-QC velocity seed is invalid")
            parameters.update(steps=steps, report_interval=interval, velocity_seed=seed)
        if request.engine == "library-first-openmm-equilibration":
            if request.resource_budget.max_candidates != 1:
                raise ValueError("OpenMM equilibration requires max_candidates=1")
            if request.resource_budget.max_runtime_minutes > 720:
                raise ValueError("OpenMM equilibration runtime budget cannot exceed 720 minutes")
            runtime = self.equilibration_runtime(parameters)
            runtime_platform = runtime.get("platform")
            threads = request.resource_budget.cpu_count
            if runtime_platform == 'CUDA':
                from .external_openmm import configured_profile, validate_budget
                profile = configured_profile(runtime)
                validate_budget(request.resource_budget)
                threads = 4
                parameters.update(scheduler_profile=profile, platform_validation_required=True)
            elif request.resource_budget.cpu_count > 8 or request.resource_budget.memory_gb > 32:
                raise ValueError("OpenMM equilibration exceeds the registered CPU or memory ceiling")
            expected_gpus = 1 if runtime_platform == "CUDA" else 0
            if request.resource_budget.gpu_count != expected_gpus:
                raise ValueError("OpenMM equilibration GPU budget does not match the registered platform")
            seeds = (int(parameters.get("integrator_seed", 20261015)),
                     int(parameters.get("barostat_seed", 20261016)))
            if any(not 1 <= seed <= 2147483647 for seed in seeds) or seeds[0] == seeds[1]:
                raise ValueError("OpenMM equilibration requires two distinct positive seeds")
            parameters.update(integrator_seed=seeds[0], barostat_seed=seeds[1],
                              duration_ps=500.0, report_interval_ps=10.0, cpu_threads=threads)
        if request.engine == "library-first-openmm-production":
            if request.resource_budget.max_candidates != 1 or request.resource_budget.max_runtime_minutes > 1440:
                raise ValueError("OpenMM production requires one candidate and at most 1440 minutes")
            if request.resource_budget.cpu_count > 8 or request.resource_budget.memory_gb > 32:
                raise ValueError("OpenMM production exceeds the registered CPU or memory ceiling")
            expected_gpus = 1 if self.config.get("openmm_production",{}).get("platform") == "CUDA" else 0
            if request.resource_budget.gpu_count != expected_gpus:
                raise ValueError("OpenMM production GPU budget does not match the registered platform")
            replica = int(parameters["replica"]); base = int(parameters.get("seed_base",20261020))
            seeds = (base+replica*3,base+replica*3+1,base+replica*3+2)
            if not 1 <= replica <= 3 or any(not 1 <= s <= 2147483647 for s in seeds):
                raise ValueError("OpenMM production replica or seed base is invalid")
            parameters.update(replica=replica,seed_base=base,velocity_seed=seeds[0],
                integrator_seed=seeds[1],barostat_seed=seeds[2],duration_ps=10000.0,
                trajectory_interval_ps=10.0)
        if request.engine == "library-first-trajectory-audit":
            if request.resource_budget.gpu_count != 0:
                raise ValueError("Trajectory audit is registered as a CPU-only job")
            if request.resource_budget.max_candidates != 1:
                raise ValueError("Trajectory audit requires max_candidates=1")
            if (request.resource_budget.max_runtime_minutes > 120
                    or request.resource_budget.cpu_count > 4
                    or request.resource_budget.memory_gb > 16):
                raise ValueError("Trajectory audit exceeds the registered resource ceiling")
        if request.engine == "library-first-structural-analysis":
            if request.resource_budget.gpu_count != 0 or request.resource_budget.max_candidates != 1:
                raise ValueError("Structural analysis requires one candidate and no GPU")
            if (request.resource_budget.max_runtime_minutes > 240
                    or request.resource_budget.cpu_count > 8
                    or request.resource_budget.memory_gb > 32):
                raise ValueError("Structural analysis exceeds the registered resource ceiling")
        if request.engine == "library-first-md-report":
            if request.resource_budget.gpu_count != 0 or request.resource_budget.max_candidates != 1:
                raise ValueError("MD report requires one candidate and no GPU")
            if (request.resource_budget.max_runtime_minutes > 30
                    or request.resource_budget.cpu_count > 2
                    or request.resource_budget.memory_gb > 4):
                raise ValueError("MD report exceeds the registered resource ceiling")
        if request.engine == "library-first-md-candidate-delivery":
            if request.resource_budget.gpu_count != 0:
                raise ValueError("MD candidate delivery is registered as a CPU-only job")
            if (request.resource_budget.max_runtime_minutes > 30
                    or request.resource_budget.cpu_count > 2
                    or request.resource_budget.memory_gb > 8
                    or request.resource_budget.max_candidates > 100):
                raise ValueError("MD candidate delivery exceeds the registered resource ceiling")
        if request.engine == "library-first-md-adduct-admission":
            if request.resource_budget.gpu_count!=0 or request.resource_budget.max_candidates!=1:
                raise ValueError("MD adduct admission requires one candidate and no GPU")
            if (request.resource_budget.max_runtime_minutes>60
                    or request.resource_budget.cpu_count>4
                    or request.resource_budget.memory_gb>16):
                raise ValueError("MD adduct admission exceeds the registered resource ceiling")
            timeout=int(parameters.get("native_timeout_seconds",1200))
            if not 60<=timeout<=min(3600,request.resource_budget.max_runtime_minutes*60):
                raise ValueError("Native graph-QC timeout exceeds its registered job budget")
            parameters["native_timeout_seconds"]=timeout
        if request.engine == "ligand-first-structure-intake":
            parameters["restore_missing"] = bool(
                parameters.get("restore_missing", True)
            )
        if request.engine == "ligand-first-covalentization":
            backend = str(parameters.get("enumeration_backend", "rdkit-branch"))
            if backend not in {"rdkit-branch", "upstream-native"}:
                raise ValueError("unknown Ligand-first enumeration backend")
            parameters["enumeration_backend"] = backend
            parameters["candidate_budget"] = request.resource_budget.max_candidates
            max_sites = int(parameters.get("max_sites", 8))
            max_branch = int(parameters.get("max_branch_heavy_atoms", 12))
            max_distance = float(parameters.get("max_distance_angstrom", 12.0))
            max_angle = float(parameters.get("max_vector_angle_degrees", 100.0))
            if not 1 <= max_sites <= 100:
                raise ValueError(
                    "ligand-first-covalentization max_sites must be between 1 and 100"
                )
            if not 1 <= max_branch <= 50:
                raise ValueError(
                    "ligand-first-covalentization max_branch_heavy_atoms must be between 1 and 50"
                )
            if not 2.0 <= max_distance <= 30.0:
                raise ValueError(
                    "ligand-first-covalentization max_distance_angstrom must be between 2 and 30"
                )
            if not 1.0 <= max_angle <= 180.0:
                raise ValueError(
                    "ligand-first-covalentization max_vector_angle_degrees must be between 1 and 180"
                )
            candidate_ceiling = max_sites * 12
            if backend == "rdkit-branch" and candidate_ceiling > request.resource_budget.max_candidates:
                raise ValueError(
                    "ligand-first-covalentization requires max_candidates >= max_sites * 12"
                )
            parameters.update(
                max_sites=max_sites,
                max_branch_heavy_atoms=max_branch,
                max_distance_angstrom=max_distance,
                max_vector_angle_degrees=max_angle,
                formal_warheads=[
                    "acrylamide",
                    "chloroacetamide",
                    "vinyl_sulfone",
                    "propiolamide",
                ],
                short_linkers=["direct", "methylene", "ethylene"],
            )
            if backend == "upstream-native":
                parameters["formal_warheads"] = ["acrylamide", "chloroacetamide", "vinyl_sulfonamide", "propiolamide"]
                parameters["short_linkers"] = ["native-upstream-rules"]
        if request.engine == "covindb-prescreen":
            if parameters.get('vs_prescreen'):
                from .library_vs_prescreen_input import validate_request
                validate_request(self.project_root, request)
                if request.execution_mode == 'execute':
                    from .library_vs_prescreen_execution import validate_request as validate_workflow
                    validate_workflow(self.project_root, request)
            top_n = int(parameters.get("top_n", 0))
            max_rows = parameters.get("max_rows")
            chunk_size = int(parameters.get("chunk_size", 5000))
            if top_n < 1:
                raise ValueError("covindb-prescreen top_n must be positive")
            if top_n > request.resource_budget.max_candidates:
                raise ValueError(
                    f"covindb-prescreen requested {top_n} candidates but max_candidates is "
                    f"{request.resource_budget.max_candidates}"
                )
            if max_rows is not None and int(max_rows) < top_n:
                raise ValueError("covindb-prescreen max_rows cannot be smaller than top_n")
            if max_rows is not None and int(max_rows) > 3_000_000:
                raise ValueError("covindb-prescreen max_rows exceeds the approved v1 source bound")
            if not 100 <= chunk_size <= 100_000:
                raise ValueError("covindb-prescreen chunk_size must be between 100 and 100000")
            parameters.update(
                top_n=top_n,
                max_rows=int(max_rows) if max_rows is not None else None,
                chunk_size=chunk_size,
                seed=int(
                    parameters.get(
                        "seed", self.config.get("runtime", {}).get("random_seed", 20260725)
                    )
                ),
            )
        if request.engine == "rdkit-warhead-enumeration":
            attachment_indices = [
                int(value) for value in parameters.get("attachment_atom_indices", [])
            ]
            protected_indices = [
                int(value) for value in parameters.get("protected_atom_indices", [])
            ]
            if any(value < 0 for value in (*attachment_indices, *protected_indices)):
                raise ValueError("RDKit enumeration atom indices must be non-negative")
            allowed_warheads = [str(value) for value in parameters.get("allowed_warheads", [])]
            supported_warheads = {
                "acrylamide",
                "chloroacetamide",
                "vinyl_sulfone",
                "propiolamide",
            }
            unsupported = sorted(set(allowed_warheads) - supported_warheads)
            if not allowed_warheads or unsupported:
                raise ValueError(
                    "RDKit enumeration allowed_warheads must use the four formal "
                    f"Covalentizer warheads; unsupported={unsupported}"
                )
            allowed_linkers = [
                str(value) for value in parameters.get("allowed_linkers", ["direct"])
            ]
            supported_linkers = {"direct", "methylene", "ethylene"}
            unsupported_linkers = sorted(set(allowed_linkers) - supported_linkers)
            if not allowed_linkers or unsupported_linkers:
                raise ValueError(
                    "RDKit enumeration supports direct, methylene, and ethylene linkers; "
                    f"unsupported={unsupported_linkers}"
                )
            max_candidates = int(
                parameters.get("max_candidates", request.resource_budget.max_candidates)
            )
            if not 1 <= max_candidates <= request.resource_budget.max_candidates:
                raise ValueError(
                    "RDKit enumeration max_candidates must fit inside the approved budget"
                )
            parameters.update(
                attachment_atom_indices=attachment_indices,
                protected_atom_indices=protected_indices,
                allowed_warheads=allowed_warheads,
                allowed_linkers=allowed_linkers,
                max_candidates=max_candidates,
            )
        if request.engine in {"pocketxmol", "reinvent4", "difflinker"}:
            allowed_routes = {
                "pocketxmol": {"de-novo"},
                "reinvent4": {"de-novo"},
                "difflinker": {"ligand-first", "de-novo"},
            }
            if request.route not in allowed_routes[request.engine]:
                raise ValueError(
                    f"{request.engine} is not registered for route {request.route} with current assets"
                )
            requested_candidates = int(parameters.get("n_candidates", 0))
            if requested_candidates < 1:
                raise ValueError("generator n_candidates must be a positive integer")
            if requested_candidates > request.resource_budget.max_candidates:
                raise ValueError(
                    f"generator requested {requested_candidates} candidates but max_candidates is "
                    f"{request.resource_budget.max_candidates}"
                )
            device = str(parameters.get("device", "cuda"))
            if device not in {"cpu", "cuda"}:
                raise ValueError("generator device must be cpu or cuda")
            cuda_device_index = int(parameters.get("cuda_device_index", 0))
            if device == "cuda" and request.resource_budget.gpu_count < 1:
                raise ValueError(f"{request.engine} with device=cuda requires gpu_count >= 1")
            if device == "cuda" and (
                cuda_device_index < 0 or cuda_device_index >= request.resource_budget.gpu_count
            ):
                raise ValueError("cuda_device_index must address one GPU inside the approved budget")
            if device == "cpu" and cuda_device_index != 0:
                raise ValueError("cuda_device_index must be 0 when generator device=cpu")
            parameters["n_candidates"] = requested_candidates
            parameters["cuda_device_index"] = cuda_device_index
            parameters["device"] = device
            parameters["seed"] = int(
                parameters.get(
                    "seed", self.config.get("runtime", {}).get("random_seed", 20260725)
                )
            )
        if request.engine == "pocketxmol":
            center = parameters.get("pocket_center")
            if not isinstance(center, (list, tuple)) or len(center) != 3:
                raise ValueError("PocketXMol pocket_center must contain exactly three coordinates")
            coordinates = [float(value) for value in center]
            if not all(math.isfinite(value) for value in coordinates):
                raise ValueError("PocketXMol pocket_center coordinates must be finite")
            radius = float(parameters.get("pocket_radius", 15.0))
            mean_atoms = int(parameters.get("mean_atoms", 28))
            std_atoms = float(parameters.get("std_atoms", 2.0))
            if not 5.0 <= radius <= 30.0:
                raise ValueError("PocketXMol pocket_radius must be between 5 and 30 angstrom")
            if not 5 <= mean_atoms <= 80 or not 0.1 <= std_atoms <= 20.0:
                raise ValueError("PocketXMol atom-count distribution is outside approved bounds")
            parameters.update(
                pocket_center=coordinates,
                pocket_radius=radius,
                mean_atoms=mean_atoms,
                std_atoms=std_atoms,
            )
        if request.engine == "difflinker":
            linker_size = str(parameters.get("linker_size", ""))
            if not re.fullmatch(r"(?:[1-9]|[1-9][0-9])(?:,(?:[1-9]|[1-9][0-9]))?", linker_size):
                raise ValueError("DiffLinker linker_size must be an integer or two comma-separated integers")
            values = [int(value) for value in linker_size.split(",")]
            if any(value > 60 for value in values) or (len(values) == 2 and values[0] > values[1]):
                raise ValueError("DiffLinker linker_size bounds must be ordered and no greater than 60")
            anchors = parameters.get("anchors")
            if anchors is not None and not re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", str(anchors)):
                raise ValueError("DiffLinker anchors must be comma-separated one-based atom indices")
            if parameters.get("n_steps") is not None:
                n_steps = int(parameters["n_steps"])
                if not 1 <= n_steps <= 1000:
                    raise ValueError("DiffLinker n_steps must be between 1 and 1000")
                parameters["n_steps"] = n_steps
            parameters["linker_size"] = linker_size
        if request.engine == "rtmscore":
            requested_limit = int(parameters.get("max_ligands", request.resource_budget.max_candidates))
            parameters["max_ligands"] = min(
                requested_limit, request.resource_budget.max_candidates
            )
        if request.engine == "carsidock-pool-selection":
            top_n = int(parameters.get("top_n", 0))
            if top_n < 1:
                raise ValueError("carsidock-pool-selection top_n must be positive")
            if top_n > request.resource_budget.max_candidates:
                raise ValueError(
                    f"carsidock-pool-selection requested {top_n} candidates but "
                    f"max_candidates is {request.resource_budget.max_candidates}"
                )
            score_table = self._within_project(str(parameters.get("carsidock_scores", "")))
            import pandas as pd

            if score_table.suffix.lower() == ".parquet":
                row_count = len(pd.read_parquet(score_table, columns=["input_sdf_index"]))
            elif score_table.suffix.lower() == ".csv":
                row_count = len(pd.read_csv(score_table, usecols=["input_sdf_index"]))
            else:
                raise ValueError("carsidock-pool-selection score table must be CSV or Parquet")
            if top_n > row_count:
                raise ValueError(
                    f"carsidock-pool-selection top_n={top_n} exceeds input rows={row_count}"
                )
            quota_keys = ("score_union_n", "diversity_n", "rescue_n")
            supplied = [parameters.get(key) is not None for key in quota_keys]
            if any(supplied) and not all(supplied):
                raise ValueError("all three recall-pool quotas must be provided together")
            if all(supplied):
                quotas = [int(parameters[key]) for key in quota_keys]
                if min(quotas) < 0 or sum(quotas) != top_n:
                    raise ValueError("recall-pool quotas must be non-negative and sum to top_n")
                parameters.update(zip(quota_keys, quotas))
            minimum = float(parameters.get("min_carsidock_success_rate", 0.75))
            if not 0.0 <= minimum <= 1.0:
                raise ValueError("min_carsidock_success_rate must be between zero and one")
            parameters.update(
                top_n=top_n,
                input_row_count=row_count,
                min_carsidock_success_rate=minimum,
            )
        if request.engine == "alphafold3-mpae":
            candidate_table = self._within_project(str(parameters.get("candidate_table", "")))
            import pandas as pd

            if candidate_table.suffix.lower() == ".parquet":
                row_count = len(pd.read_parquet(candidate_table, columns=["input_sdf_index"]))
            elif candidate_table.suffix.lower() == ".csv":
                row_count = len(pd.read_csv(candidate_table, usecols=["input_sdf_index"]))
            else:
                raise ValueError("AF3 candidate_table must be CSV or Parquet")
            top_n = int(parameters.get("top_n", 0))
            if top_n < 1 or top_n > row_count:
                raise ValueError("AF3 top_n must be positive and no greater than input rows")
            if row_count > request.resource_budget.max_candidates:
                raise ValueError(
                    f"AF3 candidate table has {row_count} rows but max_candidates is "
                    f"{request.resource_budget.max_candidates}"
                )
            reparse_only = bool(parameters.get("reuse_artifact_dir"))
            gpu = int(parameters.get("cuda_device_index", 0))
            if reparse_only:
                reuse_artifact_dir = self._within_project(
                    str(parameters["reuse_artifact_dir"])
                )
                if not (reuse_artifact_dir / "prepared" / "batch_manifest.json").is_file():
                    raise ValueError("AF3 recovery source lacks prepared/batch_manifest.json")
                if not (reuse_artifact_dir / "predictions").is_dir():
                    raise ValueError("AF3 recovery source lacks predictions directory")
                if gpu != 0:
                    raise ValueError("AF3 reparse-only cuda_device_index must be 0")
            elif gpu < 0 or gpu >= request.resource_budget.gpu_count:
                raise ValueError("AF3 cuda_device_index must address one GPU inside the budget")
            numeric_bounds = {
                "num_recycles": (1, 20, 3),
                "num_diffusion_samples": (1, 20, 1),
                "num_seeds": (1, 20, 1),
                "cpu_per_search": (1, request.resource_budget.cpu_count, 2),
            }
            for key, (minimum, maximum, default) in numeric_bounds.items():
                value = int(parameters.get(key, default))
                if not minimum <= value <= maximum:
                    raise ValueError(f"AF3 {key} must be between {minimum} and {maximum}")
                parameters[key] = value
            coverage = float(parameters.get("min_coverage", 0.95))
            if not 0.0 <= coverage <= 1.0:
                raise ValueError("AF3 min_coverage must be between zero and one")
            parameters.update(
                top_n=top_n,
                input_row_count=row_count,
                cuda_device_index=gpu,
                min_coverage=coverage,
                resume=bool(parameters.get("resume", True)),
                run_data_pipeline=bool(parameters.get("run_data_pipeline", False)),
                seed=int(
                    parameters.get(
                        "seed", self.config.get("runtime", {}).get("random_seed", 20260725)
                    )
                ),
            )
        if request.engine == "schrodinger-covdock-lo":
            candidate_table = self._within_project(
                str(parameters.get("candidate_table", ""))
            )
            import pandas as pd

            if candidate_table.suffix.lower() == ".parquet":
                identity = pd.read_parquet(
                    candidate_table, columns=["input_sdf_index"]
                )
            elif candidate_table.suffix.lower() == ".csv":
                identity = pd.read_csv(
                    candidate_table, usecols=["input_sdf_index"]
                )
            else:
                raise ValueError("CovDock-LO candidate_table must be CSV or Parquet")
            indices = pd.to_numeric(identity["input_sdf_index"], errors="raise")
            if indices.duplicated().any():
                raise ValueError("CovDock-LO candidate_table has duplicate input_sdf_index")
            row_count = len(identity)
            if parameters.get("pose_consistency_table"):
                pose_table = self._within_project(
                    str(parameters["pose_consistency_table"])
                )
                if pose_table.suffix.lower() == ".parquet":
                    pose_identity = pd.read_parquet(
                        pose_table,
                        columns=[
                            "input_sdf_index",
                            "af3_covdock_ligand_rmsd_angstrom",
                        ],
                    )
                elif pose_table.suffix.lower() == ".csv":
                    pose_identity = pd.read_csv(
                        pose_table,
                        usecols=[
                            "input_sdf_index",
                            "af3_covdock_ligand_rmsd_angstrom",
                        ],
                    )
                else:
                    raise ValueError(
                        "CovDock-LO pose_consistency_table must be CSV or Parquet"
                    )
                pose_indices = pd.to_numeric(
                    pose_identity["input_sdf_index"], errors="raise"
                )
                if pose_indices.duplicated().any():
                    raise ValueError(
                        "CovDock-LO pose_consistency_table has duplicate input_sdf_index"
                    )
                if set(pose_indices.astype(int)) != set(indices.astype(int)):
                    raise ValueError(
                        "CovDock-LO pose_consistency_table identity set differs from candidate_table"
                    )
            if row_count < 1:
                raise ValueError("CovDock-LO candidate_table is empty")
            if row_count > request.resource_budget.max_candidates:
                raise ValueError(
                    f"CovDock-LO candidate table has {row_count} rows but max_candidates is "
                    f"{request.resource_budget.max_candidates}"
                )
            final_n = int(parameters.get("final_n", 0))
            if not 1 <= final_n <= row_count:
                raise ValueError("CovDock-LO final_n must be between 1 and input row count")
            attachment = str(parameters.get("attachment_residue", ""))
            if not re.fullmatch(r"[A-Za-z0-9]+:[1-9][0-9]*", attachment):
                raise ValueError("CovDock-LO attachment_residue must use CHAIN:author_number")
            reaction_type_column = str(
                parameters.get("reaction_type_column", "")
            ).strip()
            if reaction_type_column:
                if not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]{0,127}", reaction_type_column
                ):
                    raise ValueError("CovDock-LO reaction_type_column is invalid")
                header = (
                    pd.read_parquet(candidate_table)
                    if candidate_table.suffix.lower() == ".parquet"
                    else pd.read_csv(candidate_table)
                )
                if reaction_type_column not in header:
                    raise ValueError(
                        "CovDock-LO reaction_type_column is absent from candidate_table"
                    )
                allowed_reactions = {
                    "Michael Addition",
                    "Nucleophilic Substitution",
                    "Conjugate Addition to Alkyne (carbonyl activated)",
                }
                reactions = set(
                    header[reaction_type_column].astype(str).str.strip()
                )
                unsupported = sorted(reactions - allowed_reactions)
                if unsupported:
                    raise ValueError(
                        f"CovDock-LO candidate table has unsupported reaction types: {unsupported}"
                    )
                reaction_type = "per-candidate-column"
            else:
                reaction_type = str(parameters.get("reaction_type", "")).strip()
                if not reaction_type or len(reaction_type) > 100:
                    raise ValueError(
                        "CovDock-LO reaction_type must be a non-empty bounded string"
                    )
            checkpoint_size = int(parameters.get("checkpoint_size", 0))
            if checkpoint_size < 0 or checkpoint_size > row_count:
                raise ValueError(
                    "CovDock-LO checkpoint_size must be zero or no greater than input rows"
                )
            if checkpoint_size and not reaction_type_column:
                raise ValueError(
                    "CovDock-LO candidate checkpoints require a reaction_type_column"
                )
            checkpoint_workers = int(parameters.get("checkpoint_workers", 1))
            checkpoint_worker_cpus = int(parameters.get("checkpoint_worker_cpus", 1))
            shard_count = math.ceil(row_count / checkpoint_size) if checkpoint_size else 1
            if not 1 <= checkpoint_workers <= shard_count:
                raise ValueError(
                    "CovDock-LO checkpoint_workers must fit the immutable shard count"
                )
            if checkpoint_size == 0 and checkpoint_workers != 1:
                raise ValueError(
                    "CovDock-LO parallel checkpoint workers require checkpoint_size"
                )
            if checkpoint_worker_cpus < 1:
                raise ValueError("CovDock-LO checkpoint_worker_cpus must be positive")
            if checkpoint_workers * checkpoint_worker_cpus > request.resource_budget.cpu_count:
                raise ValueError(
                    "CovDock-LO checkpoint concurrency exceeds the approved CPU budget"
                )
            njobs = int(parameters.get("njobs", min(row_count, request.resource_budget.cpu_count)))
            if not 1 <= njobs <= request.resource_budget.cpu_count:
                raise ValueError("CovDock-LO njobs must fit inside the approved CPU budget")
            if njobs > row_count:
                raise ValueError("CovDock-LO njobs cannot exceed candidate count")
            host = str(parameters.get("host", f"localhost:{njobs}"))
            if not re.fullmatch(r"localhost:[1-9][0-9]*", host):
                raise ValueError("registered CovDock-LO v1 host must use localhost:N")
            if int(host.split(":", 1)[1]) > request.resource_budget.cpu_count:
                raise ValueError("CovDock-LO host slots exceed the approved CPU budget")
            bounds = {
                "max_local_strain": (0.0, 100.0, 10.0),
                "hard_max_strain": (0.0, 200.0, 20.0),
                "min_covalent_bond_length": (1.0, 2.5, 1.5),
                "max_covalent_bond_length": (1.2, 3.0, 2.2),
                "max_pose_rmsd": (0.1, 20.0, 3.0),
                "severe_clash_distance": (0.5, 2.0, 1.2),
                "min_success_rate": (0.0, 1.0, 0.80),
            }
            normalized_bounds: dict[str, float] = {}
            for key, (minimum, maximum, default) in bounds.items():
                value = float(parameters.get(key, default))
                if not minimum <= value <= maximum:
                    raise ValueError(
                        f"CovDock-LO {key} must be between {minimum} and {maximum}"
                    )
                normalized_bounds[key] = value
            if normalized_bounds["hard_max_strain"] < normalized_bounds["max_local_strain"]:
                raise ValueError("CovDock-LO hard_max_strain cannot be below max_local_strain")
            if (
                normalized_bounds["max_covalent_bond_length"]
                <= normalized_bounds["min_covalent_bond_length"]
            ):
                raise ValueError("CovDock-LO covalent bond-length bounds are not ordered")
            if parameters.get("min_radius") is not None:
                min_radius = float(parameters["min_radius"])
                if not 1.0 <= min_radius <= 10.0:
                    raise ValueError("CovDock-LO min_radius must be between 1 and 10 angstrom")
                parameters["min_radius"] = min_radius
            parameters.update(
                input_row_count=row_count,
                final_n=final_n,
                attachment_residue=attachment,
                reaction_type=reaction_type,
                reaction_type_column=reaction_type_column or None,
                checkpoint_size=checkpoint_size,
                checkpoint_workers=checkpoint_workers,
                checkpoint_worker_cpus=checkpoint_worker_cpus,
                njobs=njobs,
                host=host,
                resume=bool(parameters.get("resume", True)),
                prepare_receptor=bool(parameters.get("prepare_receptor", False)),
                **normalized_bounds,
            )
            if parameters.get('retained_candidates'):
                if request.route != 'ligand-first' or not reaction_type_column:
                    raise ValueError('Retained recovery requires reaction-routed Ligand-first')
                from .covdock_retained import validate_retained_recovery, retained_source_parent
                retained = validate_retained_recovery(self.project_root, parameters,
                    parameters['retained_candidates'],
                    retained_source_parent(self.project_root, request.parent_run_id))
                parameters['retained_candidates'] = [
                    {'path': str(value['descriptor_path'].relative_to(self.project_root)),
                     'sha256': value['descriptor_sha256']}
                    for _, value in sorted(retained.items())]
        if request.engine in {"library-first-pareto", "ligand-first-pareto"}:
            candidate_table = self._within_project(
                str(parameters.get("candidate_table", ""))
            )
            import pandas as pd

            if candidate_table.suffix.lower() == ".parquet":
                header = pd.read_parquet(candidate_table)
            elif candidate_table.suffix.lower() == ".csv":
                header = pd.read_csv(candidate_table)
            else:
                raise ValueError("Pareto candidate_table must be CSV or Parquet")
            row_count = len(header)
            if row_count < 1:
                raise ValueError("Pareto candidate_table is empty")
            if row_count > request.resource_budget.max_candidates:
                raise ValueError(
                    f"Pareto candidate table has {row_count} rows but max_candidates is "
                    f"{request.resource_budget.max_candidates}"
                )
            final_n = int(parameters.get("final_n", 0))
            if not 1 <= final_n <= row_count:
                raise ValueError("Pareto final_n must be between 1 and input row count")
            objectives = parameters.get("additional_objectives", {})
            if not isinstance(objectives, dict):
                raise ValueError("additional_objectives must be a column-to-direction object")
            for column, direction in objectives.items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", str(column)):
                    raise ValueError(f"invalid additional objective column: {column}")
                if direction not in {"min", "max"}:
                    raise ValueError(f"invalid objective direction for {column}: {direction}")
                if column not in header:
                    raise ValueError(f"additional Pareto objective is absent: {column}")
            parameters.update(
                input_row_count=row_count,
                final_n=final_n,
                additional_objectives=dict(objectives),
                _pareto_profile=(
                    "ligand-first"
                    if request.engine == "ligand-first-pareto"
                    else "library-first"
                ),
            )
        if request.engine == "ligand-first-evaluation":
            candidate_table = self._within_project(
                str(parameters.get("candidate_table", ""))
            )
            import pandas as pd

            if candidate_table.suffix.lower() == ".parquet":
                identity = pd.read_parquet(
                    candidate_table, columns=["input_sdf_index"]
                )
            elif candidate_table.suffix.lower() == ".csv":
                identity = pd.read_csv(
                    candidate_table, usecols=["input_sdf_index"]
                )
            else:
                raise ValueError(
                    "Ligand-first evaluation candidate_table must be CSV or Parquet"
                )
            if identity.empty or identity["input_sdf_index"].duplicated().any():
                raise ValueError(
                    "Ligand-first evaluation identity denominator must be non-empty and unique"
                )
            if len(identity) > request.resource_budget.max_candidates:
                raise ValueError(
                    "Ligand-first evaluation candidate count exceeds approved budget"
                )
            minimum = float(parameters.get("min_parent_mcs_fraction", 0.60))
            if not 0.1 <= minimum <= 1.0:
                raise ValueError(
                    "min_parent_mcs_fraction must be between 0.1 and 1.0"
                )
            parameters.update(
                input_row_count=len(identity),
                min_parent_mcs_fraction=minimum,
            )
        if request.engine == "carsidock-cov":
            ligand_table = self._within_project(str(parameters.get("ligand_table", "")))
            import pandas as pd
            from rdkit import Chem

            reaction_type_column = str(
                parameters.get("reaction_type_column", "")
            ).strip()
            columns = ["input_sdf_index"] + (
                [reaction_type_column] if reaction_type_column else []
            )
            if ligand_table.suffix.lower() == ".parquet":
                identity_frame = pd.read_parquet(
                    ligand_table, columns=columns
                )
            elif ligand_table.suffix.lower() in {".csv", ".tsv"}:
                separator = "\t" if ligand_table.suffix.lower() == ".tsv" else ","
                identity_frame = pd.read_csv(
                    ligand_table, sep=separator, usecols=columns
                )
            else:
                raise ValueError("carsidock ligand_table must be CSV, TSV, or Parquet")
            table_indices = identity_frame["input_sdf_index"].astype(int)
            if table_indices.duplicated().any():
                raise ValueError("carsidock ligand_table has duplicate input_sdf_index values")
            if reaction_type_column:
                if not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]{0,127}", reaction_type_column
                ):
                    raise ValueError("carsidock reaction_type_column is invalid")
                supported = {
                    "Michael Addition",
                    "Nucleophilic Addition to a Double Bond",
                    "Nucleophilic Addition to a Triple Bond",
                    "Nucleophilic Substitution",
                    "Boronic Acid Addition",
                    "Epoxide/Aziridine Opening",
                    "Imine Condensation",
                    "Beta Lactone/Lactam Addition",
                    "Gamma Lactone/Lactam Addition",
                    "Disulfide Formation",
                    "Others",
                }
                reactions = set(
                    identity_frame[reaction_type_column].astype(str).str.strip()
                )
                unsupported = sorted(reactions - supported)
                if unsupported:
                    raise ValueError(
                        f"carsidock ligand table has unsupported reaction types: {unsupported}"
                    )
            ligand_sdf = self._within_project(str(parameters.get("ligand_sdf", "")))
            sdf_indices: list[int] = []
            unreadable_records = 0
            for molecule in Chem.SDMolSupplier(
                str(ligand_sdf), removeHs=False, sanitize=False
            ):
                if molecule is None or not molecule.HasProp("INPUT_SDF_INDEX"):
                    unreadable_records += 1
                    continue
                sdf_indices.append(int(molecule.GetProp("INPUT_SDF_INDEX")))
            if unreadable_records:
                raise ValueError(
                    f"carsidock ligand_sdf has {unreadable_records} unreadable or unidentified records"
                )
            if len(sdf_indices) != len(set(sdf_indices)):
                raise ValueError("carsidock ligand_sdf has duplicate INPUT_SDF_INDEX values")
            if set(sdf_indices) != set(table_indices.tolist()):
                missing = sorted(set(table_indices.tolist()) - set(sdf_indices))[:20]
                unknown = sorted(set(sdf_indices) - set(table_indices.tolist()))[:20]
                raise ValueError(
                    "carsidock ligand_table/SDF identity mismatch: "
                    f"missing_in_sdf={missing}, unknown_in_sdf={unknown}"
                )
            row_count = len(identity_frame)
            if row_count > request.resource_budget.max_candidates:
                raise ValueError(
                    f"ligand_table has {row_count} rows but max_candidates is "
                    f"{request.resource_budget.max_candidates}"
                )
            parameters["input_row_count"] = row_count
        if request.engine == "library-first-evaluation":
            score_table = self._within_project(str(parameters.get("score_table", "")))
            import pandas as pd

            if score_table.suffix.lower() == ".parquet":
                row_count = len(pd.read_parquet(score_table, columns=["input_sdf_index"]))
            elif score_table.suffix.lower() == ".csv":
                row_count = len(pd.read_csv(score_table, usecols=["input_sdf_index"]))
            else:
                raise ValueError("evaluation score_table must be CSV or Parquet")
            if row_count > request.resource_budget.max_candidates:
                raise ValueError(
                    f"score_table has {row_count} rows but max_candidates is "
                    f"{request.resource_budget.max_candidates}"
                )
            parameters["input_row_count"] = row_count
        command, inputs, outputs = builders[request.engine](run_id, parameters)
        if (request.engine == "library-first-md-candidate-delivery"
                and parameters["candidate_count"] > request.resource_budget.max_candidates):
            raise ValueError("MD candidate count exceeds the approved resource budget")
        if parameters.get('dispatch_policy') == 'ligand-hpc-retained-v1' and request.attempt == 4:
            inputs.extend(str(self.registry.job_dir(request.parent_run_id) / name)
                          for name in ('spec.json', 'result.json', 'stderr.log'))
        input_sha256 = {
            (str(Path(path).resolve()) if request.engine == 'library-first-openmm-equilibration'
             and path in parameters.get('runtime_artifacts', {})
             else config_reference(self.project_root, path) if Path(path).suffix in {".yaml", ".yml"}
             else str(Path(path).resolve().relative_to(self.project_root))): sha256_file(Path(path))
            for path in inputs
            if Path(path).is_file()
        }
        git_head = subprocess.run(
            ["git", "-C", str(self.project_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        provenance: dict[str, Any] = {
            "python_executable": self._python(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "project_git_head": git_head or None,
            "config_path": "configs/local.inspire.yaml",
        }
        if request.engine == 'schrodinger-covdock-vs':
            provenance.update(python_executable=sys.executable,
                algorithm_version='covdock-vs-preparation-and-docking-v1',covdock_mode='enrichment',
                scientific_acceptance=False,md_ready=False)
        from .source_release import frozen_release
        release = frozen_release()
        if release:
            provenance["source_release"] = release
        config_path = self.project_root / "configs" / "local.inspire.yaml"
        if config_path.is_file():
            provenance["config_sha256"] = sha256_file(config_path)
        if request.engine == "library-first-receptor-preparation":
            runtime = self.config.get("receptor_preparation", {})
            provenance.update({
                "algorithm_version": "library-first-receptor-preparation-v1",
                "audit_python": os.path.realpath(str(runtime.get("audit_python", ""))),
                "openmm_python": os.path.realpath(str(runtime.get("openmm_python", ""))),
                "openmm_pythonpath": os.path.realpath(
                    str(runtime.get("openmm_pythonpath", ""))),
                "pdbfixer_source": "bundled_release_hashed",
                "prepared_scope": "isolated_selected_chain_protein",
                "campaign_ready": False,
                "runtime_versions": parameters["runtime_versions"],
            })
        if request.engine == "shared-deepcosi-site-prediction":
            provenance.update({"algorithm_version": "shared-deepcosi-site-prediction-v1",
                "scope": "cysteine-site-ranking-not-reactivity-probability",
                "runtime_registration": parameters["runtime_registration"]})
        if request.engine == "library-first-adduct-parameterization":
            provenance.update({"algorithm_version": "library-first-adduct-parameterization-v1",
                "forcefield": "OPLS_2005", "water_model": "TIP3P",
                "simulation_state": "covalent_adduct", "md_system_ready": False,
                "runtime_registration": parameters["runtime_registration"]})
        if request.engine == "library-first-openmm-system-preparation":
            provenance.update({"algorithm_version": "library-first-openmm-system-preparation-v1",
                "forcefield": "OPLS_2005", "water_model": "TIP3P",
                "simulation_state": "covalent_adduct", "short_qc_completed": False,
                "md_system_ready": False, "runtime_versions": parameters["runtime_versions"],
                "runtime_pythonpath": parameters["runtime_pythonpath"]})
        if request.engine == "library-first-openmm-short-qc":
            provenance.update({"algorithm_version": "library-first-openmm-short-qc-v1",
                "ensemble": "NVT", "unrestrained": True, "timestep_fs": 1.0,
                "equilibration_completed": False, "md_system_ready": False,
                "runtime_versions": parameters["runtime_versions"],
                "runtime_pythonpath": parameters["runtime_pythonpath"]})
        if request.engine == "library-first-openmm-equilibration":
            provenance.update({"algorithm_version": "library-first-openmm-equilibration-v1",
                "ensemble": "NPT", "unrestrained": True, "duration_ps": 500.0,
                "timestep_fs": 2.0, "equilibrium_convergence_claimed": False,
                "production_started": False, "md_system_ready": False,
                "runtime_versions": parameters["runtime_versions"],
                "runtime_pythonpath": parameters["runtime_pythonpath"]})
        if request.engine == "library-first-openmm-production":
            provenance.update({"algorithm_version":"library-first-openmm-production-v1",
                "ensemble":"NPT","duration_ps":10000.0,"timestep_fs":2.0,
                "trajectory_interval_ps":10.0,"trajectory_audited":False,
                "analysis_completed":False,"runtime_versions":parameters["runtime_versions"],
                "runtime_pythonpath":parameters["runtime_pythonpath"]})
        if request.engine == "library-first-trajectory-audit":
            provenance.update({"algorithm_version":"library-first-trajectory-audit-v1",
                "integrity_scope":"DCD structure, hashes, frame sequence and covalent distance",
                "structural_analysis_completed":False,"binding_stability_claimed":False,
                "affinity_claimed":False,"runtime_versions":parameters["runtime_versions"],
                "runtime_pythonpath":parameters["runtime_pythonpath"]})
        if request.engine == "library-first-structural-analysis":
            provenance.update({"algorithm_version":"library-first-structural-analysis-v1",
                "replica_policy":"exactly replicas 1, 2 and 3 from one equilibration parent",
                "descriptive_only":True,"convergence_claimed":False,"affinity_claimed":False,
                "screening_decision_made":False,
                "runtime_pythonpath":parameters["runtime_pythonpath"]})
        if request.engine == "library-first-md-report":
            provenance.update({"algorithm_version":"library-first-covalent-md-report-v1",
                "policy_id":parameters["policy_id"],"expert_review_required":True,
                "experimental_activity_claimed":False,"affinity_claimed":False,
                "runtime_pythonpath":parameters["runtime_pythonpath"]})
        if request.engine == "library-first-md-candidate-delivery":
            provenance.update({"algorithm_version":"library-first-md-candidate-delivery-v1",
                "selection_not_made":True,"native_graph_qc_completed":False,
                "raw_paths_exposed":False,"candidate_count":parameters["candidate_count"]})
        if request.engine == "library-first-md-adduct-admission":
            provenance.update({"algorithm_version":"library-first-md-adduct-admission-v1",
                "native_graph_qc":"Schrodinger StructureReader extraction and independent re-read",
                "parameterization_completed":False,"md_system_ready":False,
                "runtime_registration":parameters["runtime_registration"]})
            if "discovery_origin" in parameters:
                provenance["discovery_origin"] = dict(parameters["discovery_origin"])
        if request.engine in {"carsidock-cov", "rtmscore"}:
            carsidock = self.config.get("carsidock", {})
            checkpoint_key = "checkpoint" if request.engine == "carsidock-cov" else "rtmscore_checkpoint"
            checkpoint = Path(carsidock.get(checkpoint_key, ""))
            provenance["engine_repo"] = carsidock.get("repo")
            provenance["checkpoint"] = str(checkpoint)
            if checkpoint.is_file():
                provenance["checkpoint_sha256"] = sha256_file(checkpoint)
        if request.engine == "covindb-prescreen":
            source = Path(str(self.config.get("covindb", {}).get("virtual_screening_library", "")))
            if parameters.get('vs_prescreen'):
                from .library_vs_prescreen_input import load
                bound, _ = load(self.project_root, parameters['vs_prescreen'])
                source = self.project_root / bound['roles']['library']
            provenance.update(
                {
                    "dataset": "CovalentInDB 2.0 Virtual Screening Library",
                    "dataset_path": str(source),
                    "dataset_size_bytes": source.stat().st_size,
                    "algorithm_version": "covindb-prescreen-v1.3",
                }
            )
        if request.engine == "carsidock-pool-selection":
            provenance.update(
                {
                    "algorithm_version": "carsidock-recall-pool-v1.0",
                    "selection_policy": "score_union+scaffold_diversity+failure_rescue",
                }
            )
        if request.engine == "alphafold3-mpae":
            af3 = self.config.get("alphafold3", {})
            runner = Path(str(af3.get("runner", "")))
            model_files = sorted(Path(str(af3.get("model_dir", ""))).glob("*.bin"))
            provenance.update(
                {
                    "algorithm_version": "af3-mpae-selection-v1.0",
                    "af3_runner": str(runner),
                    "af3_runner_sha256": sha256_file(runner) if runner.is_file() else None,
                    "af3_python": af3.get("python"),
                    "af3_model_dir": af3.get("model_dir"),
                    "af3_model_files": [
                        {"name": path.name, "size_bytes": path.stat().st_size}
                        for path in model_files
                    ],
                    "af3_database_dir": af3.get("database_dir"),
                    "primary_score": "interface_mpae_lower_is_better",
                    "reparse_only": bool(request.parameters.get("reuse_artifact_dir")),
                    "recovery_source_run_id": request.parameters.get(
                        "recovery_source_run_id"
                    ),
                }
            )
            if "refinement_input_contract" in parameters:
                provenance["refinement_input_contract"] = parameters["refinement_input_contract"]
            provenance["af3_target_seed_check"] = parameters["af3_target_seed_check"]
        if request.engine == "schrodinger-covdock-lo":
            from .schrodinger import validate_license_config

            schrodinger = self.config.get("schrodinger", {})
            root = Path(str(schrodinger.get("root", "")))
            license_file = Path(str(schrodinger.get("license_file", "")))
            license_ok, license_reason = validate_license_config(str(license_file))
            provenance.update(
                {
                    "algorithm_version": "covdock-lo-registered-v1.2",
                    "gate_algorithm_version": parameters.get("gate_algorithm_version", "covdock-lo-pose-gate-v2.1"),
                    "schrodinger_root": str(root),
                    "schrodinger_release": root.name,
                    "license_configured": license_file.is_file(),
                    "license_preflight": license_reason,
                    "license_file_sha256": (
                        sha256_file(license_file) if license_file.is_file() else None
                    ),
                    "license_content_redacted": True,
                    "covdock_mode": "leadopt",
                    "primary_order_within_gate": "input core order" if parameters.get("input_score_source") == "core" else "AF3-mPAE rank",
                    "covdock_score_role": "supporting_pose_evidence",
                }
            )
            if "input_score_source" in parameters:
                provenance["input_score_source"] = parameters["input_score_source"]
            if "covdock_receptor_input_check" in parameters:
                provenance["covdock_receptor_input_check"] = parameters["covdock_receptor_input_check"]
            if "refinement_input_contract" in parameters:
                provenance["refinement_input_contract"] = parameters["refinement_input_contract"]
            if not license_ok:
                raise ValueError(f"Schrödinger license preflight failed: {license_reason}")
        if request.engine == "ligand-first-covalentization":
            provenance.update(
                {
                    "algorithm_version": "ligand-first-covalentization-v1.0",
                    "site_method": "deterministic-covalentizer-rdkit-branch-replacement",
                    "attachment_planner_used": False,
                    "warhead_count": 4,
                    "short_linker_count": 3,
                    "chemistry_gate_scope": (
                        "sanitization, valence, radicals, charge, single component, "
                        "protected parent core, exactly one mapped warhead"
                    ),
                }
            )
            if parameters.get("enumeration_backend") == "upstream-native":
                provenance.update(algorithm_version="upstream-covalentizer-native-v1",
                                  site_method="unmodified upstream reactions on full parent",
                                  short_linker_count=None,
                                  chemistry_gate_scope="sanitization, single component, radicals, silicon removal, full-parent graph retention, declared electrophile")
        if request.engine == "ligand-first-structure-intake":
            provenance.update(
                {
                    "algorithm_version": "ligand-first-structure-intake-v1.0",
                    "source_policy": "frozen campaign assets; missing-only RCSB HTTPS restore; SHA-256 required",
                    "structure_claim": "input reconstruction and mapping audit only",
                }
            )
        if request.engine == "ligand-first-evaluation":
            provenance.update(
                {
                    "algorithm_version": "ligand-first-independent-evaluation-v1.0",
                    "independent_reparse": True,
                    "uses_saved_gate_label_as_truth": False,
                    "experimental_claim": False,
                }
            )
        if request.engine in {"library-first-pareto", "ligand-first-pareto"}:
            provenance.update(
                {
                    "algorithm_version": (
                        "ligand-first-pareto-v1.0"
                        if request.engine == "ligand-first-pareto"
                        else "library-first-pareto-v2.0"
                    ),
                    "applied_route": request.route,
                    "selection_method": "non_dominated_sorting_with_scaffold_round_robin",
                    "weighted_sum_used": False,
                    "gate_labels_are_objectives": False,
                    "hard_filter": "physical validity only",
                    "manual_review_required_before_purchase": True,
                }
            )
        if request.engine in {"pocketxmol", "reinvent4", "difflinker"}:
            repo, generator_python, checkpoint = self._generator_assets(request.engine)
            provenance.update(
                {
                    "generator_repo": str(repo),
                    "generator_python": str(generator_python),
                    "checkpoint": str(checkpoint),
                    "checkpoint_sha256": sha256_file(checkpoint),
                    "proposal_scope": "generation_only_not_binding_or_reactivity_evidence",
                }
            )
        frozen_ligand_preparation = request.route == 'ligand-first' and (
            request.engine == 'library-first-receptor-preparation' or
            request.engine == 'schrodinger-covdock-lo' and bool(parameters.get('workflow_id')))
        frozen_ligand_preparation = frozen_ligand_preparation or (
            request.route == 'de-novo' and request.engine == 'schrodinger-covdock-lo'
            and bool(parameters.get('workflow_id')))
        frozen_ligand_preparation = frozen_ligand_preparation or request.engine == 'de-novo-prereaction-vina'
        frozen_ligand_preparation = frozen_ligand_preparation or request.engine == 'schrodinger-covdock-vs'
        frozen_ligand_preparation = frozen_ligand_preparation or parameters.get('dispatch_policy') in {'library-vs-lo-v1', 'library-lo-instance-v1'}
        frozen_ligand_preparation = frozen_ligand_preparation or (request.engine == 'covindb-prescreen' and bool(parameters.get('vs_prescreen')))
        frozen_ligand_preparation = frozen_ligand_preparation or (
            request.engine in {'library-first-md-adduct-admission', 'library-first-adduct-parameterization',
                'library-first-openmm-system-preparation', 'library-first-openmm-short-qc',
                'library-first-openmm-equilibration'} and bool(parameters.get('workflow_id')))
        if not release and (frozen_ligand_preparation or parameters.get('dispatch_policy') in {
            'ligand-smoke-v1', 'ligand-hpc-sharded-v1', 'ligand-hpc-retained-v1'
        }):
            # Frozen source and runtime config are part of native approval, not
            # merely a mutable deployment pointer read by the later GPU worker.
            for folder in ('src/covscreen', 'scripts'):
                for path in sorted((self.project_root / folder).rglob('*.py')):
                    path = path.resolve()
                    input_sha256[str(path.relative_to(self.project_root))] = sha256_file(path)
        if self._configuration_bytes is not None and (frozen_ligand_preparation or parameters.get('dispatch_policy') in {
            'ligand-smoke-v1', 'ligand-hpc-sharded-v1', 'ligand-hpc-retained-v1'
        }):
            from .runtime_config import snapshot
            frozen_config = snapshot(self.project_root, self._configuration_bytes)
            reference = str(frozen_config.relative_to(self.project_root))
            input_sha256[reference] = sha256_file(frozen_config)
            inputs.append(str(frozen_config))
            provenance.update(config_path=reference, config_sha256=input_sha256[reference],
                              config_binding='immutable-runtime-config-v1')
        # The job supervisor can use an isolated host interpreter while each
        # scientific command retains its own registered engine environment.
        # Include this choice in the exact approval, never select it after signing.
        worker = self.config.get('runtime', {}).get('worker_python')
        if worker is not None and self.worker_backend(request) == 'local':
            if (not isinstance(worker, str) or not Path(worker).is_absolute()
                    or not Path(worker).is_file() or not os.access(worker, os.X_OK)):
                raise ValueError('Registered worker Python is unavailable')
            provenance.update(worker_python_executable=worker, worker_python_sha256=sha256_file(Path(worker)))
        spec = JobSpec(
            run_id=run_id,
            request_id=request.request_id,
            project_id=request.project_id,
            route=request.route,
            engine=request.engine,
            command=command,
            working_directory=str(self.project_root),
            output_directory=str(self.registry.job_dir(run_id) / "artifacts"),
            parameters=parameters,
            input_paths=inputs,
            input_sha256=input_sha256,
            expected_output_paths=outputs,
            provenance=provenance,
            resource_budget=request.resource_budget,
            execution_mode=request.execution_mode,
            human_approved=request.human_approved,
            approval_note=request.approval_note,
            parent_run_id=request.parent_run_id,
            attempt=request.attempt,
        )
        if parameters.get('dispatch_policy') == 'ligand-hpc-retained-v1':
            from .covdock_retained import validate_retained_spec
            validate_retained_spec(self.project_root, spec)
        if spec.engine == 'rdkit-warhead-enumeration':
            payload = self._rdkit_request_payload(spec)
            encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()
            request_path = self.registry.job_dir(spec.run_id) / 'request.json'
            spec.input_sha256[str(request_path.relative_to(self.project_root))] = hashlib.sha256(encoded).hexdigest()
        from .job_cancellation import PROTOCOL
        spec.provenance['cancellation_protocol'] = PROTOCOL
        from .branch_guard import require_unquarantined
        require_unquarantined(self.project_root, spec)
        return spec

    def submit(self, request: JobSubmissionRequest) -> RunResult:
        from .submission_identity import submit_once
        from .branch_guard import execution_barrier, require_unquarantined
        spec = self._prepare_submission(request)
        with execution_barrier(self.project_root):
            require_unquarantined(self.project_root, spec)
            return submit_once(self, request, lambda value: self._submit_registered(value, spec),
                               planned_run_id=spec.run_id)

    def _prepare_submission(self, request: JobSubmissionRequest) -> JobSpec:
        from .docking_policy import require_engine_enabled
        require_engine_enabled(request.engine, route=request.route)

        backend = self.worker_backend(request)
        if backend not in {"local", "external-hpc", "external-inspire", "external-openmm", "external-instance"}:
            raise ValueError("Unknown registered worker backend")
        if backend == "external-hpc" and request.engine not in {"schrodinger-covdock-lo", "schrodinger-covdock-vs"} and not (
                request.engine == 'covindb-prescreen' and request.parameters.get('vs_prescreen')):
            raise ValueError("External HPC currently supports only registered CovDock jobs")
        spec = self.build_spec(request)
        spec.provenance["worker_backend"] = backend
        if spec.engine == 'de-novo-prereaction-glide':
            from .layout import enable_de_novo
            enable_de_novo()
            from covscreen_de_novo.batch_glide import validate_boundary
            validate_boundary(spec)
        if backend == 'external-instance' and spec.engine == 'schrodinger-covdock-lo':
            from .instance_lo_claim import validate_boundary
            validate_boundary(spec, spec.parameters.get('execution_instance', 'asgbie3'))
        if backend == 'external-openmm':
            from .external_openmm import validate_boundary
            validate_boundary(spec)
        if backend == 'external-inspire':
            from .external_inspire import validate_boundary
            validate_boundary(spec)
        if backend == 'external-hpc':
            from .external_hpc import validate_boundary
            validate_boundary(spec.model_copy(update={'execution_mode': 'execute', 'human_approved': True})
                              if spec.execution_mode == 'plan' else spec)
        from .native_approval import CURRENT, seal_spec
        current_covdock = (request.engine == 'schrodinger-covdock-lo' and request.route == 'ligand-first'
                           and request.parameters.get('workflow_id'))
        current_denovo = (request.engine == 'schrodinger-covdock-lo' and request.route == 'de-novo'
                          and request.parameters.get('workflow_id'))
        current_prereaction = request.engine == 'de-novo-prereaction-vina'
        if request.engine == 'de-novo-prereaction-glide' and spec.execution_mode == 'execute':
            from .layout import enable_de_novo
            enable_de_novo()
            from covscreen_de_novo.batch_glide import validate_compiled_spec
            validate_compiled_spec(self.project_root, spec)
        if current_prereaction and spec.execution_mode == 'execute':
            from .layout import enable_de_novo
            enable_de_novo()
            from covscreen_de_novo.prereaction_workflow import validate_compiled_spec
            validate_compiled_spec(self.project_root, spec)
        if current_denovo and spec.execution_mode == 'execute':
            from .layout import enable_de_novo
            enable_de_novo()
            if request.parameters.get('denovo_batch_lo_sha256'):
                from covscreen_de_novo.batch_lo import validate_compiled_spec
            else:
                from covscreen_de_novo.independent_review import validate_compiled_spec
            validate_compiled_spec(self.project_root, spec)
        current_graph = request.engine == 'library-first-md-adduct-admission' and request.parameters.get('workflow_id')
        current_parameterization = request.engine == 'library-first-adduct-parameterization' and request.parameters.get('workflow_id')
        current_preparation = request.engine in {'library-first-openmm-system-preparation', 'library-first-openmm-short-qc', 'library-first-openmm-equilibration'} and request.parameters.get('workflow_id')
        if current_preparation and spec.execution_mode == 'execute':
            from .ligand_md_preparation import require_dispatch_authority
            require_dispatch_authority(self.project_root, request)
        if current_parameterization and spec.execution_mode == 'execute':
            from .ligand_md_parameterization import require_dispatch_authority
            require_dispatch_authority(self.project_root, request)
        if current_graph and spec.execution_mode == 'execute':
            from .ligand_md_graph_execution import require_dispatch_authority
            require_dispatch_authority(self.project_root, request)
        if spec.execution_mode == 'execute' and (backend in {'external-hpc', 'external-inspire', 'external-openmm', 'external-instance'} or CURRENT.get() or current_covdock or current_denovo or current_prereaction or current_graph or current_parameterization or current_preparation):
            spec.provenance['native_approval'] = seal_spec(spec)
        return spec

    def _submit_registered(self, request: JobSubmissionRequest, spec: JobSpec) -> RunResult:
        from .dashboard import try_refresh_dashboard
        backend = spec.provenance['worker_backend']
        job_dir = self.registry.job_dir(spec.run_id)
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        result = RunResult(
            run_id=spec.run_id,
            state="PLANNED" if spec.execution_mode == "plan" else "QUEUED",
            engine=spec.engine,
            route=spec.route,
            project_id=spec.project_id,
            attempt=spec.attempt,
            parent_run_id=spec.parent_run_id,
            submitted_at=utc_now(),
            message=(
                "Execution plan compiled; no command launched."
                if spec.execution_mode == "plan"
                else "Approved job persisted and queued for background execution."
            ),
            stdout_path=str(stdout_path.relative_to(self.project_root)),
            stderr_path=str(stderr_path.relative_to(self.project_root)),
        )
        self.registry.create(spec, result)
        try_refresh_dashboard(self.project_root, spec.run_id)
        if spec.engine == "rdkit-warhead-enumeration":
            request_payload = self._rdkit_request_payload(spec)
            self.registry._write_json(job_dir / "request.json", request_payload)
            request_path = job_dir / "request.json"
            if sha256_file(request_path) != spec.input_sha256[str(request_path.relative_to(self.project_root))]:
                raise ValueError('Derived enumeration request differs from frozen approval input')
        if spec.execution_mode == "plan":
            return result

        if backend in {"external-hpc", "external-inspire", "external-openmm", "external-instance"}:
            result.message = "Approved job queued for external dispatch; no compute launched yet."
            self.registry.write_result(result)
            try_refresh_dashboard(self.project_root, spec.run_id)
            return result

        worker_python = spec.provenance.get('worker_python_executable', self._python())
        if ('worker_python_sha256' in spec.provenance
                and sha256_file(Path(worker_python)) != spec.provenance['worker_python_sha256']):
            raise ValueError('Approved worker Python changed')
        worker_command = [
            worker_python,
            str(science_script("run_job_worker.py")),
            "--project-root", str(self.project_root),
            "--run-id", spec.run_id,
        ]
        environment = _worker_environment(self.project_root)
        subprocess.Popen(
            worker_command,
            cwd=self.project_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # After spawn, only the worker owns lifecycle writes. Publishing its
        # PID from a stale QUEUED read could overwrite RUNNING or even a fast
        # terminal result. The worker records its own PID before engine launch.
        return self.registry.load_result(spec.run_id)

    def retry(
        self,
        run_id: str,
        *,
        human_approved: bool,
        approval_note: str,
        execution_mode: str = "execute",
    ) -> RunResult:
        return self.submit(
            self.compile_retry_request(
                run_id,
                human_approved=human_approved,
                approval_note=approval_note,
                execution_mode=execution_mode,
            )
        )

    def compile_retry_request(
        self,
        run_id: str,
        *,
        human_approved: bool,
        approval_note: str,
        execution_mode: str = "execute",
    ) -> JobSubmissionRequest:
        """Compile the sole allowed next attempt without registering it.

        Retry ancestry is immutable.  The bounded Ligand-first CovDock smoke
        run has two deliberately narrow technical recoveries: the known
        Schrödinger 2021-2 parallel Job Control crash is first retried as a
        fresh serial invocation; if that exact attempt reaches the scheduler's
        three-hour limit, its three immutable candidate shards may be retried
        concurrently under a newly approved aggregate CPU/memory ceiling.
        Scientific inputs and reaction chemistry remain unchanged.
        """
        previous_spec = self.registry.load_spec(run_id)
        from .docking_policy import require_engine_enabled
        require_engine_enabled(previous_spec.engine, route=previous_spec.route)
        previous_result = self.registry.load_result(run_id)
        if previous_result.state not in {"FAILED", "TIMED_OUT", "CANCELLED"}:
            raise ValueError(
                f"job {run_id} cannot be retried from state {previous_result.state}"
            )
        parameters = dict(previous_spec.parameters)
        is_bounded_covdock_recovery = (
            previous_spec.engine == "schrodinger-covdock-lo"
            and previous_spec.provenance.get("worker_backend") == "external-inspire"
            and parameters.get("dispatch_policy") == "ligand-smoke-v1"
        )
        if is_bounded_covdock_recovery:
            is_parallel_engine_recovery = False
            if (
                previous_spec.attempt == 1
                and previous_result.state == "FAILED"
                and previous_result.failure_reason == "engine_return_code_4"
                and isinstance(parameters.get("njobs"), int)
                and parameters["njobs"] > 1
            ):
                is_parallel_engine_recovery = True
                parameters.update(njobs=1, host="localhost:1", resume=False)
            elif (
                previous_spec.attempt == 2
                and previous_result.state == "TIMED_OUT"
                and previous_result.failure_reason == "external_scheduler_timed_out"
                and int(parameters.get("input_row_count", 0)) == 3
                and int(parameters.get("checkpoint_size") or 0) in {0, 1}
                and int(parameters.get("njobs", 0)) == 1
            ):
                parameters.update(
                    dispatch_policy="ligand-hpc-sharded-v1",
                    checkpoint_size=1,
                    checkpoint_workers=3,
                    checkpoint_worker_cpus=4,
                    njobs=1,
                    host="localhost:1",
                    resume=False,
                )
                return JobSubmissionRequest(
                    request_id=f"{previous_spec.request_id}-retry-3",
                    project_id=previous_spec.project_id,
                    route=previous_spec.route,
                    engine=previous_spec.engine,
                    parameters=parameters,
                    resource_budget=ResourceBudget(
                        max_runtime_minutes=180,
                        gpu_count=0,
                        cpu_count=12,
                        memory_gb=48,
                        max_candidates=3,
                    ),
                    execution_mode=execution_mode,
                    human_approved=human_approved,
                    approval_note=approval_note,
                    parent_run_id=run_id,
                    attempt=3,
                )
            if not is_parallel_engine_recovery:
                raise ValueError(
                    "CovDock smoke recovery is allowed only after the verified "
                    "parallel engine failure or the bounded serial scheduler timeout"
                )
        return JobSubmissionRequest(
            request_id=f"{previous_spec.request_id}-retry-{previous_spec.attempt + 1}",
            project_id=previous_spec.project_id,
            route=previous_spec.route,
            engine=previous_spec.engine,
            parameters=parameters,
            resource_budget=previous_spec.resource_budget,
            execution_mode=execution_mode,
            human_approved=human_approved,
            approval_note=approval_note,
            parent_run_id=run_id,
            attempt=previous_spec.attempt + 1,
        )

    def cancel(self, run_id: str) -> RunResult:
        from .job_cancellation import request
        return request(self.registry, run_id)


def _artifact_role(path: Path) -> str:
    name = path.name.lower()
    if name.endswith((".stdout.log", ".stderr.log")) or path.suffix == ".log":
        return "log"
    if "manifest" in name or name == "spec.json" or name == "result.json":
        return "manifest"
    if "candidate" in name:
        return "candidate_table"
    if "score" in name or path.suffix == ".parquet":
        return "score_table"
    if "metric" in name or "report" in name or path.suffix.lower() == ".md":
        return "report"
    if path.suffix.lower() in {".sdf", ".pdb", ".mol2"}:
        return "pose"
    return "other"


def _worker_environment(project_root: Path) -> dict[str, str]:
    """Build the minimal environment inherited by the detached job worker."""
    allowed = {
        "HOME",
        "PATH",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "DGLBACKEND",
        "LANG",
        "LC_ALL",
        "COVALENT_APPROVAL_KEY_FILE",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        f":{environment['PYTHONPATH']}" if environment.get("PYTHONPATH") else ""
    )
    return environment


_PREFERRED_CANDIDATE_TABLES: dict[str, tuple[str, ...]] = {
    "covindb-prescreen": ("stage2_candidates.parquet", "stage2_candidates.csv"),
    "ligand-first-covalentization": (
        "ligand_first_candidates.parquet",
        "ligand_first_candidates.csv",
    ),
    "carsidock-cov": ("carsidock_scores.parquet", "carsidock_scores.csv"),
    "rtmscore": ("rtmscore.parquet", "rtmscore.csv"),
    "carsidock-pool-selection": (
        "stage3_recall_pool.parquet",
        "stage3_recall_pool.csv",
    ),
    "alphafold3-mpae": (
        "stage4_top_candidates.parquet",
        "stage4_top_candidates.csv",
        "af3_scores.parquet",
    ),
    "schrodinger-covdock-lo": (
        "stage5_gated_candidates.parquet",
        "stage5_gated_candidates.csv",
        "covdock_lo_scores.parquet",
    ),
    "ligand-first-evaluation": (
        "ligand_first_evaluated_candidates.parquet",
        "ligand_first_evaluated_candidates.csv",
    ),
    "ligand-first-pareto": (
        "stage6_final_candidates.parquet",
        "stage6_final_candidates.csv",
        "stage6_pareto_front.parquet",
        "stage6_pareto_front.csv",
    ),
    "library-first-pareto": (
        "stage6_final_candidates.parquet",
        "stage6_final_candidates.csv",
        "stage6_pareto_front.parquet",
        "stage6_pareto_front.csv",
        "stage6_pareto_all_candidates.parquet",
        "stage6_pareto_all_candidates.csv",
    ),
    "library-first-md-candidate-delivery": ("candidate-results.json",),
    "library-first-md-adduct-admission": ("md-adduct-admission.json","adduct-input/adduct-input.json"),
}


def _select_candidate_table_path(
    engine: str, artifacts: list[ArtifactRecord]
) -> str | None:
    tables = [
        artifact.path
        for artifact in artifacts
        if artifact.role in {"candidate_table", "score_table"}
        and Path(artifact.path).suffix.lower() in {".csv", ".parquet", ".json"}
    ]
    by_name = {Path(path).name: path for path in tables}
    for preferred in _PREFERRED_CANDIDATE_TABLES.get(engine, ()):
        if preferred in by_name:
            return by_name[preferred]
    return tables[0] if tables else None


def _engine_environment(spec: JobSpec, project_root: Path) -> dict[str, str]:
    """Build a deterministic engine environment instead of trusting the submitter shell.

    In particular, the relocated CarsiDock Python runtime links against ICU 78 and must load
    its matching GCC runtime before Ubuntu's system libstdc++.  Background submissions do not
    inherit `environment/activate_carsidock.sh`, so this invariant belongs in the worker.
    """

    if spec.engine == 'schrodinger-covdock-vs':
        environment = {key: os.environ[key] for key in (
            'HOME','PATH','LANG','LC_ALL','SLURM_JOB_ID','COVALENT_APPROVAL_KEY_FILE') if key in os.environ}
        environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                           PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
        return environment
    if spec.engine == 'de-novo-prereaction-vina':
        from .layout import enable_de_novo
        enable_de_novo()
        from covscreen_de_novo.prereaction_workflow import environment
        return environment()
    allowed = {
        "HOME",
        "PATH",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "DGLBACKEND",
        "LANG",
        "LC_ALL",
        "CUDA_VISIBLE_DEVICES",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    engine_threads = (spec.parameters['cpu_threads'] if spec.provenance.get('worker_backend') == 'external-openmm'
                      else spec.resource_budget.cpu_count)
    for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENMM_CPU_THREADS'):
        environment[name] = str(engine_threads)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        f":{environment['PYTHONPATH']}" if environment.get("PYTHONPATH") else ""
    )
    if spec.engine in {
        "library-first-openmm-system-preparation", "library-first-openmm-short-qc",
        "library-first-openmm-equilibration", "library-first-openmm-production",
        "library-first-trajectory-audit", "library-first-structural-analysis",
        "library-first-md-report",
    }:
        runtime_pythonpath = Path(str(spec.parameters.get("runtime_pythonpath", ""))).resolve()
        if runtime_pythonpath.is_symlink() or not runtime_pythonpath.is_dir():
            raise ValueError("Persisted OpenMM Python path is unavailable")
        environment.pop("PYTHONHOME", None)
        environment.pop("LD_LIBRARY_PATH", None)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(Path(__file__).resolve().parents[1]), str(runtime_pythonpath)))
    # AF3's project wrapper uses the same Python 3.11 orchestrator; its native
    # AF3 subprocess is separately isolated by af3_environment.py.
    if spec.engine in {"carsidock-cov", "rtmscore", "alphafold3-mpae"}:
        configured_python = Path(str(spec.provenance.get("python_executable", spec.command[0])))
        if len(configured_python.parents) < 3:
            raise ValueError("cannot derive CarsiDock shared runtime root from python executable")
        shared_root = configured_python.parents[2]
        runtime_lib = shared_root / "python311_runtime" / "lib"
        carsidock_lib = shared_root / "carsidock_runtime_libs" / "lib"
        required = [runtime_lib, carsidock_lib]
        missing = [str(path) for path in required if not path.is_dir()]
        if missing:
            raise ValueError(f"required CarsiDock runtime libraries are missing: {missing}")
        # The legacy venv records /opt/conda in pyvenv.cfg. GPU images may
        # contain a different build there: force the matching shared stdlib,
        # just as activate_carsidock.sh does, without mutating the shared venv.
        runtime_home = shared_root / 'python311_runtime'
        environment['PYTHONHOME'] = str(runtime_home)
        environment['PYTHONNOUSERSITE'] = '1'
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        environment['PYTHONPATH'] = os.pathsep.join([
            str(Path(__file__).resolve().parents[1]),
            str(shared_root / 'carsidock_gpu_env/lib/python3.11/site-packages'),
        ])
        environment["LD_LIBRARY_PATH"] = ":".join(
            [
                str(runtime_lib),
                str(carsidock_lib),
                "/usr/lib/x86_64-linux-gnu",
                "/lib/x86_64-linux-gnu",
            ]
        )
        environment["PATH"] = str(configured_python.parent) + (
            f":{environment['PATH']}" if environment.get("PATH") else ""
        )
        environment["DGLBACKEND"] = "pytorch"
        environment["PYTHONUNBUFFERED"] = "1"
    if spec.engine == "schrodinger-covdock-lo":
        import getpass

        environment.pop("LD_LIBRARY_PATH", None)
        environment.pop("LD_PRELOAD", None)
        environment.pop("LD_AUDIT", None)
        # This is the project wrapper, not the vendor process. It needs its
        # registered shared packages on HPC; vendor launchers sanitize separately.
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
            os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
        )
        environment.pop("PYTHONHOME", None)
        root = str(spec.provenance.get("schrodinger_root", ""))
        license_path = str(
            next(
                (
                    value
                    for index, value in enumerate(spec.command)
                    if index > 0 and spec.command[index - 1] == "--license-file"
                ),
                "",
            )
        )
        if not root or not license_path:
            raise ValueError("CovDock-LO registered environment lacks licensed runtime provenance")
        environment["SCHRODINGER"] = root
        environment["SCHROD_LICENSE_FILE"] = license_path
        username = os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()
        environment["USER"] = username
        environment["LOGNAME"] = username
        environment["PYTHONUNBUFFERED"] = "1"
    return environment


def execute_persisted_job(project_root: str | Path, run_id: str) -> RunResult:
    """Claim one queued run across processes; observation never restarts a run.

    A dead worker's RUNNING record is unresolved, not permission to relaunch.
    An authorized retry must register a new run through the normal boundary.
    """
    import fcntl

    root = Path(project_root).resolve()
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run_id)
    if spec.execution_mode != 'execute' or not spec.human_approved:
        raise ValueError('persisted spec is not approved for execution')
    if spec.provenance.get('worker_backend') == 'external-instance':
        raise ValueError('Instance jobs require their dedicated instance executor')

    def observe():
        result = registry.load_result(run_id)
        if any(getattr(result, field) != getattr(spec, field)
               for field in ('run_id', 'engine', 'project_id', 'route')):
            raise ValueError('Job result identity does not match its registered spec')
        return result

    descriptor = os.open(registry.job_dir(run_id) / 'worker.lock',
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return observe()
        result = observe()
        if result.state != 'QUEUED':
            return result
        return _execute_claimed_job(root, run_id)


def _execute_claimed_job(project_root: str | Path, run_id: str, *, instance_runner=None) -> RunResult:
    from .dashboard import try_refresh_dashboard

    project_root = Path(project_root).resolve()
    registry = ArtifactRegistry(project_root)
    spec = registry.load_spec(run_id)
    result = registry.load_result(run_id)
    if spec.execution_mode != "execute" or not spec.human_approved:
        raise ValueError("persisted spec is not approved for execution")
    from .vendor_budget import require_budget_control
    require_budget_control(spec, spec.provenance.get('worker_backend', 'local'))
    from .source_release import verify_input, verify_source
    verify_source(spec)
    from .stage_boundary import require_prescreen_parent
    require_prescreen_parent(project_root, spec)
    for name, digest in spec.input_sha256.items():
        verify_input(project_root, name, digest, spec)
    job_dir = registry.job_dir(run_id)
    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"
    from .job_cancellation import requested, wait as wait_cancellable, JobCancelled
    if requested(registry, run_id):
        result.state = 'CANCELLED'
        result.finished_at = utc_now()
        result.message = 'Cancellation confirmed before engine launch.'
        result.failure_reason = 'authorized_cancellation_before_launch'
        registry.write_result(result)
        return result
    from .docking_policy import require_engine_enabled
    try:
        require_engine_enabled(spec.engine, route=spec.route)
    except ValueError as error:
        result.state = "FAILED"
        result.finished_at = utc_now()
        result.failure_reason = "docking_engine_disabled"
        result.message = str(error)
        registry.write_result(result)
        return result
    Path(spec.output_directory).mkdir(parents=True, exist_ok=True)
    result.state = "RUNNING"
    result.started_at = utc_now()
    result.pid = os.getpid()
    result.message = "Registered engine command is running."
    registry.write_result(result)
    try_refresh_dashboard(project_root, run_id)
    started = time.monotonic()
    process = None
    try:
        if instance_runner is not None:
            from .branch_guard import require_unquarantined
            require_unquarantined(project_root, spec)
            return_code = instance_runner(registry, spec)
        else:
            with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
                from .branch_guard import execution_barrier, require_unquarantined
                with execution_barrier(project_root):
                    require_unquarantined(project_root, spec)
                    process = subprocess.Popen(
                        spec.command,
                        cwd=spec.working_directory,
                        env=_engine_environment(spec, project_root),
                        stdout=stdout,
                        stderr=stderr,
                        start_new_session=True,
                    )
                result.engine_pid = process.pid
                registry.write_result(result)
                try_refresh_dashboard(project_root, run_id)
                try:
                    return_code = wait_cancellable(process, registry, run_id,
                        timeout=spec.resource_budget.max_runtime_minutes * 60)
                except subprocess.TimeoutExpired:
                    # A wrapper can exit on TERM while a descendant ignores it.
                    # Clean up the whole original process group before publishing
                    # TIMED_OUT, just as for an authorized cancellation.
                    from .job_cancellation import terminate
                    terminate(process, grace_seconds=30)
                    raise
                except JobCancelled:
                    raise
                except Exception:
                    # Invalid cancellation metadata must fail closed without leaving
                    # an unobserved engine running after the worker reports failure.
                    from .job_cancellation import terminate
                    terminate(process)
                    raise
        result.return_code = return_code
        result.state = "COMPLETED" if return_code == 0 else "FAILED"
        result.message = "Job completed." if return_code == 0 else "Registered engine returned a non-zero code."
        if return_code != 0:
            result.failure_reason = f"engine_return_code_{return_code}"
    except JobCancelled:
        result.state = 'CANCELLED'
        result.return_code = process.returncode if process is not None else None
        result.failure_reason = 'authorized_cancellation'
        result.message = 'Worker confirmed engine termination after authorized cancellation.'
    except subprocess.TimeoutExpired:
        result.state = "TIMED_OUT"
        result.return_code = process.returncode if process is not None else None
        result.failure_reason = "resource_budget_runtime_exceeded"
        result.message = "Job exceeded its approved runtime budget."
    except Exception as exc:
        result.state = "FAILED"
        result.failure_reason = f"{type(exc).__name__}: {exc}"
        result.message = "Job worker failed before normal completion."

    result.runtime_seconds = time.monotonic() - started
    result.finished_at = utc_now()
    files_with_roles: list[tuple[Path, str]] = [
        (stdout_path, "log"),
        (stderr_path, "log"),
        (job_dir / "spec.json", "manifest"),
    ]
    files_with_roles.extend((Path(path), "input") for path in spec.input_paths)
    for expected in spec.expected_output_paths:
        path = Path(expected)
        if path.is_file():
            files_with_roles.append((path, _artifact_role(path)))
        elif path.is_dir():
            files_with_roles.extend(
                (item, _artifact_role(item)) for item in path.rglob("*") if item.is_file()
            )
    unique: dict[str, tuple[Path, str]] = {
        str(path.resolve()): (path.resolve(), role)
        for path, role in files_with_roles
        if path.is_file()
    }
    artifacts: list[ArtifactRecord] = []
    for path, role in list(unique.values())[:2000]:
        try:
            artifacts.append(registry.register_file(run_id, path, role))
        except (OSError, ValueError):
            continue
    result.artifacts = artifacts
    result.candidate_table_path = _select_candidate_table_path(
        spec.engine, artifacts
    )
    registry.write_result(result)
    try_refresh_dashboard(project_root, run_id)
    return result


def default_budget(engine: str) -> ResourceBudget:
    defaults = {
        "library-first-receptor-preparation": ResourceBudget(max_runtime_minutes=15, cpu_count=1, memory_gb=4, max_candidates=1),
        "library-first-adduct-parameterization": ResourceBudget(max_runtime_minutes=30, cpu_count=2, memory_gb=8, max_candidates=1),
        "library-first-openmm-system-preparation": ResourceBudget(max_runtime_minutes=30, cpu_count=2, memory_gb=16, max_candidates=1),
        "library-first-openmm-short-qc": ResourceBudget(max_runtime_minutes=30, cpu_count=2, memory_gb=16, max_candidates=1),
        "library-first-openmm-equilibration": ResourceBudget(max_runtime_minutes=720, gpu_count=1, cpu_count=4, memory_gb=32, max_candidates=1),
        "library-first-openmm-production": ResourceBudget(max_runtime_minutes=1440, gpu_count=1, cpu_count=4, memory_gb=32, max_candidates=1),
        "library-first-trajectory-audit": ResourceBudget(max_runtime_minutes=120, cpu_count=4, memory_gb=16, max_candidates=1),
        "library-first-structural-analysis": ResourceBudget(max_runtime_minutes=240, cpu_count=8, memory_gb=32, max_candidates=1),
        "library-first-md-report": ResourceBudget(max_runtime_minutes=30, cpu_count=2, memory_gb=4, max_candidates=1),
        "library-first-md-candidate-delivery": ResourceBudget(max_runtime_minutes=30, cpu_count=2, memory_gb=8, max_candidates=100),
        "library-first-md-adduct-admission": ResourceBudget(max_runtime_minutes=60, cpu_count=4, memory_gb=16, max_candidates=1),
        "covindb-prescreen": ResourceBudget(max_runtime_minutes=720, cpu_count=8, memory_gb=32, max_candidates=20000),
        "rdkit-warhead-enumeration": ResourceBudget(max_runtime_minutes=5, max_candidates=2000),
        "ligand-first-structure-intake": ResourceBudget(max_runtime_minutes=15, cpu_count=1, memory_gb=4, max_candidates=1),
        "ligand-first-covalentization": ResourceBudget(max_runtime_minutes=15, cpu_count=2, memory_gb=8, max_candidates=96),
        "carsidock-cov": ResourceBudget(max_runtime_minutes=240, gpu_count=1, cpu_count=4, memory_gb=32, max_candidates=1000),
        "rtmscore": ResourceBudget(max_runtime_minutes=120, gpu_count=1, cpu_count=4, memory_gb=32, max_candidates=10000),
        "carsidock-pool-selection": ResourceBudget(max_runtime_minutes=30, cpu_count=4, memory_gb=16, max_candidates=2000),
        "alphafold3-mpae": ResourceBudget(max_runtime_minutes=2880, gpu_count=1, cpu_count=4, memory_gb=64, max_candidates=2000),
        "schrodinger-covdock-lo": ResourceBudget(max_runtime_minutes=2880, cpu_count=55, memory_gb=128, max_candidates=100),
        "ligand-first-evaluation": ResourceBudget(max_runtime_minutes=15, cpu_count=2, memory_gb=8, max_candidates=100),
        "ligand-first-pareto": ResourceBudget(max_runtime_minutes=15, cpu_count=2, memory_gb=8, max_candidates=100),
        "library-first-pareto": ResourceBudget(max_runtime_minutes=15, cpu_count=2, memory_gb=8, max_candidates=100),
        "library-first-evaluation": ResourceBudget(max_runtime_minutes=15, cpu_count=2, memory_gb=8, max_candidates=1000000),
        "pocketxmol": ResourceBudget(max_runtime_minutes=120, gpu_count=1, cpu_count=4, memory_gb=32, max_candidates=100),
        "reinvent4": ResourceBudget(max_runtime_minutes=30, gpu_count=1, cpu_count=2, memory_gb=16, max_candidates=1000),
        "difflinker": ResourceBudget(max_runtime_minutes=120, gpu_count=1, cpu_count=4, memory_gb=32, max_candidates=100),
    }
    if engine not in defaults:
        raise ValueError(f"no default budget for engine: {engine}")
    return defaults[engine]
