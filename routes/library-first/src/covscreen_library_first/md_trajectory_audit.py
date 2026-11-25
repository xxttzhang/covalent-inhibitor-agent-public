"""Dependency-light integrity audit for OpenMM DCD production trajectories."""
from __future__ import annotations
import json,math,struct
from pathlib import Path
from .md_system_conversion import sha
from .md_system_minimization import _regular

def _record(handle,endian):
    raw=handle.read(4)
    if not raw:return None
    if len(raw)!=4:raise ValueError("Truncated DCD record marker")
    size=struct.unpack(endian+'i',raw)[0]
    if size<0 or size>2_000_000_000:raise ValueError("Invalid DCD record size")
    data=handle.read(size); tail=handle.read(4)
    if len(data)!=size or len(tail)!=4 or struct.unpack(endian+'i',tail)[0]!=size:
        raise ValueError("Truncated or mismatched DCD record")
    return data

def inspect_dcd(path,expected_atoms,covalent_indices_1based):
    import numpy as np
    path=Path(path); pair=[i-1 for i in covalent_indices_1based]
    with path.open('rb') as h:
        marker=h.read(4)
        if len(marker)!=4:raise ValueError("DCD header is missing")
        little,big=struct.unpack('<i',marker)[0],struct.unpack('>i',marker)[0]
        endian='<' if little==84 else '>' if big==84 else None
        if endian is None:raise ValueError("Unsupported DCD endian/header")
        h.seek(0); header=_record(h,endian)
        if len(header)!=84 or header[:4]!=b'CORD':raise ValueError("Invalid DCD coordinate header")
        control=struct.unpack(endian+'20i',header[4:])
        declared_frames,start_step,stride,total_steps=control[:4]
        title=_record(h,endian); atoms_record=_record(h,endian)
        if title is None or atoms_record is None or len(atoms_record)!=4:raise ValueError("DCD title/atom record missing")
        atoms=struct.unpack(endian+'i',atoms_record)[0]
        if atoms!=expected_atoms or any(i<0 or i>=atoms for i in pair):raise ValueError("DCD atom count or covalent indices mismatch")
        frames=[]; coordinate_bytes=atoms*4
        while True:
            first=_record(h,endian)
            if first is None:break
            cell=None
            if len(first) in {24,48,56}:
                cell=first; first=_record(h,endian)
            if first is None or len(first)!=coordinate_bytes:raise ValueError("DCD X coordinate record has wrong size")
            yraw=_record(h,endian); zraw=_record(h,endian)
            if yraw is None or zraw is None or len(yraw)!=coordinate_bytes or len(zraw)!=coordinate_bytes:
                raise ValueError("DCD Y/Z coordinate record has wrong size")
            dtype=np.dtype(endian+'f4')
            x=np.frombuffer(first,dtype=dtype); y=np.frombuffer(yraw,dtype=dtype); z=np.frombuffer(zraw,dtype=dtype)
            if not (np.isfinite(x).all() and np.isfinite(y).all() and np.isfinite(z).all()):
                raise ValueError("DCD contains non-finite coordinates")
            values=(x[pair[0]],y[pair[0]],z[pair[0]],x[pair[1]],y[pair[1]],z[pair[1]])
            if not all(map(math.isfinite,values)):raise ValueError("DCD contains non-finite covalent coordinates")
            distance=math.sqrt(sum((values[i]-values[i+3])**2 for i in range(3)))
            if not 1.4<distance<2.4:raise ValueError("DCD covalent distance left the structural range")
            if cell is not None:
                fmt='d' if len(cell)==48 else 'f'
                unitcell=struct.unpack(endian+f'{len(cell)//struct.calcsize(fmt)}{fmt}',cell)
                if not all(map(math.isfinite,unitcell)):raise ValueError("DCD unit cell is non-finite")
            frames.append(distance)
    if not frames:
        raise ValueError("DCD frame/step counts contain no coordinate frames")
    if declared_frames!=len(frames) or total_steps!=start_step+stride*(len(frames)-1):
        raise ValueError("DCD header frame/step counts are inconsistent")
    return {'atoms':atoms,'frames':len(frames),'start_step':start_step,'stride_steps':stride,
        'total_steps':total_steps,'covalent_distance_min_A':min(frames),
        'covalent_distance_max_A':max(frames),'endian':'little' if endian=='<' else 'big'}

def audit_production_trajectory(parameter_audit,parameter_audit_sha256,
                                production_directory,production_manifest_sha256,output,
                                *,allow_diagnostic=False):
    parameter_path=_regular(parameter_audit,parameter_audit_sha256,"Parameter audit")
    parameter=json.loads(parameter_path.read_text()); folder=Path(production_directory)
    manifest_path=_regular(folder/'production.json',production_manifest_sha256,"Production manifest")
    manifest=json.loads(manifest_path.read_text())
    allowed=("PRODUCTION_ENGINE_DIAGNOSTIC_COMPLETE_NOT_SCIENTIFIC_TRAJECTORY" if allow_diagnostic
             else "PRODUCTION_REPLICA_COMPLETE_TRAJECTORY_AUDIT_PENDING")
    if (manifest.get('schema')!='openmm-production-replica-artifact-v1' or manifest.get('status')!=allowed
            or manifest.get('system_id')!=parameter.get('system_id')
            or manifest.get('parents',{}).get('parameter_audit_sha256')!=parameter_audit_sha256
            or (not allow_diagnostic and manifest.get('production_completed') is not True)
            or manifest.get('trajectory_audited') is not False):
        raise ValueError("Production trajectory parent is not auditable")
    dcd=_regular(folder/'trajectory.dcd',manifest['artifacts']['trajectory.dcd'],"DCD trajectory")
    rows_path=_regular(folder/'frames.jsonl',manifest['artifacts']['frames.jsonl'],"Production frame log")
    pair=parameter.get('covalent_bond',{}).get('atom_indices_1based')
    if not isinstance(pair,list) or len(pair)!=2:raise ValueError("Parameter audit lacks covalent indices")
    dcd_info=inspect_dcd(dcd,parameter['total_atoms'],pair)
    rows=[json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
    expected=manifest['protocol']['frames']; interval=manifest['protocol']['trajectory_interval_ps']
    if (len(rows)!=expected or dcd_info['frames']!=expected
            or any(row.get('frame')!=i+1 or not math.isclose(row.get('time_ps'),(i+1)*interval,
                   rel_tol=0,abs_tol=1e-9) for i,row in enumerate(rows))):
        raise ValueError("DCD and frame log counts/times differ")
    output=Path(output)
    if output.exists() or output.is_symlink():raise ValueError("Trajectory audit output must be new")
    output.mkdir(parents=True)
    result={'schema':'openmm-trajectory-integrity-audit-v1',
        'status':'DIAGNOSTIC_TRAJECTORY_INTEGRITY_VERIFIED_NOT_SCIENTIFIC' if allow_diagnostic else 'TRAJECTORY_INTEGRITY_VERIFIED_ANALYSIS_PENDING',
        'system_id':parameter['system_id'],'replica':manifest['replica'],
        'parents':{'parameter_audit_sha256':parameter_audit_sha256,
            'production_manifest_sha256':production_manifest_sha256,'trajectory_sha256':sha(dcd)},
        'dcd':dcd_info,'expected_interval_ps':interval,'frame_log_sha256':sha(rows_path),
        'integrity_verified':True,'structural_analysis_completed':False,
        'binding_stability_claimed':False,'affinity_claimed':False}
    (output/'trajectory-audit.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    return result
