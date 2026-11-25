#!/usr/bin/env python3
"""Run registered native pose graph QC and admit its exact covalent adduct."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess

import _bootstrap
from covscreen.jobs import sha256_file
from covscreen_library_first.md_adduct_input import admit_covalent_adduct_input
from covscreen_library_first.md_candidate_pose import load_candidate_pose_input


def regular(path, kind="file"):
    path=Path(path)
    valid=path.is_dir() if kind=="directory" else path.is_file()
    if path.is_symlink() or not valid:
        raise ValueError("Registered native runtime input is unavailable: "+str(path))
    return path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--artifact-id",required=True)
    parser.add_argument("--manifest-sha256",required=True)
    parser.add_argument("--launcher",type=Path,required=True)
    parser.add_argument("--vendor-root",type=Path,required=True)
    parser.add_argument("--license-file",type=Path,required=True)
    parser.add_argument("--runtime-lib-dir",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--timeout-seconds",type=int,default=1200)
    args=parser.parse_args()
    launcher=regular(args.launcher); vendor=regular(args.vendor_root,"directory")
    license_file=regular(args.license_file); runtime_lib=regular(args.runtime_lib_dir,"directory")
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("MD adduct admission output must be new")
    if not 60<=args.timeout_seconds<=3600:
        raise ValueError("Native graph-QC timeout must be in [60, 3600] seconds")
    loaded=load_candidate_pose_input(args.root,args.artifact_id,args.manifest_sha256)
    args.output.mkdir(parents=True,exist_ok=False)
    handoff=args.output/"native-handoff"
    identity=pwd.getpwuid(os.geteuid()).pw_name
    environment={key:value for key,value in os.environ.items()
        if key in {"HOME","PATH","LANG","LC_ALL","TMPDIR"}}
    environment.update(USER=identity,LOGNAME=identity,SCHRODINGER=str(vendor),
        SCHRODINGER_LICENSE_FILE=str(license_file),LD_LIBRARY_PATH=str(runtime_lib),
        OMP_NUM_THREADS="2",OPENBLAS_NUM_THREADS="2",MKL_NUM_THREADS="2",
        PYTHONDONTWRITEBYTECODE="1")
    inspect=Path(__file__).with_name("inspect_covdock_md_handoff.py")
    command=[str(launcher),"python3","-B",str(inspect),"--root",str(args.root),
        "--artifact-id",args.artifact_id,"--manifest-sha256",args.manifest_sha256,
        "--output",str(handoff)]
    completed=subprocess.run(command,cwd=args.output,env=environment,
        stdin=subprocess.DEVNULL,timeout=args.timeout_seconds)
    if completed.returncode:
        return completed.returncode
    handoff_manifest=handoff/"candidate-md-handoff.json"
    handoff_value=json.loads(handoff_manifest.read_text())
    if (handoff_value.get("status")!="native_graph_qc_passed_admission_pending"
            or handoff_value.get("native_graph_qc_completed") is not True):
        raise ValueError("Native handoff did not pass graph QC")
    directory=loaded["directory"]; artifacts=handoff_value["artifacts"]
    admitted=args.output/"adduct-input"
    value=admit_covalent_adduct_input(handoff/"adduct.mae",artifacts["adduct.mae"],
        handoff/"graph-qc.json",artifacts["graph-qc.json"],
        handoff/"candidate-evidence.json",artifacts["candidate-evidence.json"],
        directory/"prepared-receptor.json",loaded["manifest"]["parents"]["prepared_receptor_sha256"],
        directory/"receptor-context.json",loaded["manifest"]["parents"]["receptor_context_sha256"],
        admitted)
    result={"schema":"library-first-md-adduct-admission-v1",
        "status":"ADDUCT_ADMITTED_PARAMETERIZATION_PENDING",
        "system_id":value["system_id"],"candidate_id":value["candidate_id"],
        "parents":{"candidate_pose_manifest_sha256":args.manifest_sha256,
            "native_handoff_manifest_sha256":sha256_file(handoff_manifest)},
        "artifacts":{"adduct-input/adduct-input.json":sha256_file(admitted/"adduct-input.json")},
        "native_graph_qc_completed":True,"parameterization_completed":False,
        "md_system_ready":False,"calculation_submitted":False}
    (args.output/"md-adduct-admission.json").write_text(
        json.dumps(result,indent=2,allow_nan=False)+"\n")
    print(json.dumps(result,allow_nan=False))
    return 0


if __name__=="__main__": raise SystemExit(main())
