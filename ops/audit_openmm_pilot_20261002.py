"""Independent DCD/coordinate audit and descriptive (not affinity) pilot analysis."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shlex
import struct
import numpy as np


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record(handle):
    prefix = handle.read(4)
    if not prefix:
        return None
    length, = struct.unpack('<i', prefix)
    assert 0 < length < 10000000
    data = handle.read(length)
    assert len(data) == length and handle.read(4) == prefix, 'Truncated DCD record'
    return data


def fit(moving, reference):
    mc, rc = moving.mean(axis=0), reference.mean(axis=0)
    u, _, vt = np.linalg.svd((moving - mc).T @ (reference - rc))
    correction = np.eye(3)
    correction[2, 2] = np.linalg.det(u @ vt)
    return u @ correction @ vt, mc, rc


def rmsd(x, y):
    return float(np.sqrt(np.mean(np.sum((x - y) ** 2, axis=1))) * 10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Downloaded raw pilot output')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, help='Frozen input directory for this candidate/attempt')
    parser.add_argument('--source-cms', type=Path, help='Original hashed CMS, for auditing beside remote data')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = args.inputs or root / 'reports/first-md-20261001/open-engine-20261002/gpu-pilot-v1'
    cfg = json.loads((base / 'protocol.json').read_text())
    name = cfg['candidate']
    assert name in {'denovo_004', 'denovo_001', 'denovo_007', 'control_afatinib'}
    cms = args.source_cms or root / 'reports/first-md-20261001' / ('parameterized-004' if name == 'denovo_004' else 'batch/' + name) / 'parameterized.cms'
    assert digest(cms) == cfg['source_cms_sha256']
    count, solute = cfg['atoms'], cfg['solute_atoms']
    beta, sulfur = cfg['covalent_atom_indices_0based']
    lines = cms.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f'm_atom[{count}] {{')
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == ':::')
    headers = [line.strip() for line in lines[start + 1:end] if line.strip() and not line.lstrip().startswith('#')]
    atoms = [dict(zip(headers, shlex.split(line)[1:])) for line in lines[end + 1:end + solute + 1]]
    ref = np.array([[float(atom[k]) * .1 for k in ['r_m_x_coord', 'r_m_y_coord', 'r_m_z_coord']] for atom in atoms])
    assert atoms[sulfur]['s_m_pdb_atom_name'].strip() == 'SG' and atoms[sulfur]['i_m_residue_number'] == '797'
    assert atoms[sulfur]['s_m_chain_name'] == 'A' and int(atoms[sulfur]['i_m_atomic_number']) == 16
    assert atoms[beta]['s_m_pdb_residue_name'].strip() == 'LIG' and int(atoms[beta]['i_m_atomic_number']) == 6
    ca = np.array([i for i, a in enumerate(atoms) if a['s_m_chain_name'] == 'A' and a['s_m_pdb_atom_name'].strip() == 'CA'])
    lig = np.array([i for i, a in enumerate(atoms) if a['s_m_pdb_residue_name'].strip() == 'LIG' and int(a['i_m_atomic_number']) > 1])
    assert len(ca) == 321 and len(lig) > 10
    pocket = ca[np.min(np.linalg.norm(ref[ca, None, :] - ref[lig][None, :, :], axis=2), axis=1) <= 1.5]
    assert len(pocket) >= 3
    finished = json.loads((args.output / 'finished.json').read_text())
    assert finished['status'] == 'PILOT_FINISHED_REQUIRES_INDEPENDENT_TRAJECTORY_AUDIT'
    assert finished['manifest_sha256'] == digest(base / 'input-manifest.json')
    assert finished['runner_sha256'] == digest(base / 'run.py')
    progress = [json.loads(line) for line in (args.output / 'progress.jsonl').read_text().splitlines()]
    prod = [row for row in progress if row['stage'] == 'production']
    frames = sorted(args.output.glob('solute-frame-*.npz'))
    assert len(prod) == len(frames) == 100
    assert np.allclose([p['time_ps'] for p in prod], np.arange(270, 1261, 10), atol=1e-5)
    summary_rows = []
    mass = np.load(base / 'source-atom-arrays.npz')['mass_da'].sum()
    with (args.output / 'production.dcd').open('rb') as handle:
        header = record(handle)
        assert len(header) == 84 and header[:4] == b'CORD'
        nframes, first_step, interval = struct.unpack('<3i', header[4:16])
        timestep_ps = struct.unpack('<f', header[40:44])[0] * .04888821
        assert (nframes, first_step, interval) == (100, 5000, 5000)
        assert abs(timestep_ps - .002) < 1e-9
        assert struct.unpack('<i', header[44:48])[0] == 1
        record(handle)  # title record
        natoms, = struct.unpack('<i', record(handle))
        assert natoms == count
        for index, (frame, scalar) in enumerate(zip(frames, prod)):
            cell = np.frombuffer(record(handle), dtype='<f8')
            assert len(cell) == 6 and np.isfinite(cell).all()
            xyz = np.stack([np.frombuffer(record(handle), dtype='<f4') for _ in range(3)], axis=1) * .1
            assert xyz.shape == (count, 3) and np.isfinite(xyz).all()
            data = np.load(frame)
            assert np.allclose(xyz[:solute], data['positions_nm'], atol=3e-6, rtol=0)
            assert abs(float(data['time_ps']) - scalar['time_ps']) < 1e-8
            box = data['box_nm']
            assert np.allclose(cell[[0, 2, 5]] * .1, np.linalg.norm(box, axis=1), atol=1e-6)
            distance = float(np.linalg.norm(xyz[beta] - xyz[sulfur]) * 10)
            assert abs(distance - scalar['covalent_distance_angstrom']) < 3e-5
            rotation, center, origin = fit(xyz[ca], ref[ca])
            protein_rmsd = rmsd((xyz[ca] - center) @ rotation + origin, ref[ca])
            rotation, center, origin = fit(xyz[pocket], ref[pocket])
            ligand_rmsd = rmsd((xyz[lig] - center) @ rotation + origin, ref[lig])
            summary_rows.append({'production_time_ps': (index + 1) * 10,
                'covalent_distance_angstrom': distance, 'protein_CA_RMSD_angstrom': protein_rmsd,
                'ligand_heavy_RMSD_after_pocket_CA_fit_angstrom': ligand_rmsd,
                'temperature_K': scalar['temperature_K'], 'potential_kj_mol': scalar['potential_kj_mol'],
                'density_g_ml': float(mass * .00166053906660 / np.linalg.det(box))})
        assert record(handle) is None, 'Unexpected extra DCD records'
    numeric = {key: {'min': min(row[key] for row in summary_rows),
                    'mean': float(np.mean([row[key] for row in summary_rows])),
                    'max': max(row[key] for row in summary_rows)} for key in summary_rows[0] if key != 'production_time_ps'}
    args.report.mkdir()
    with (args.report / 'trajectory-measurements.csv').open('w') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)
    result = {'status': 'RAW_TRAJECTORY_INTEGRITY_VERIFIED', 'candidate': name, 'frames': 100, 'atoms': count,
        'production_ps': 1000, 'prior_heating_ps': 60, 'prior_npt_ps': 200,
        'ligand_heavy_atoms': len(lig), 'pocket_CA_count': len(pocket),
        'reference': 'Original parameterized CMS, same solute coordinates as CovDock',
        'protein_fit': 'All 321 protein CA; ligand RMSD uses fixed 15 A pocket CA selection, no ligand self-fit',
        'time_convention': 'DCD starts at production 10 ps; state times include preceding 260 ps',
        'measurements': numeric, 'equilibration_converged': False, 'binding_stability_established': False,
        'limitations': 'One 1 ns run is a pipeline pilot. No replicated binding-stability or affinity conclusion.',
        'files': {name: digest(args.output / name) for name in ['production.dcd', 'finished.json', 'progress.jsonl']},
        'auditor_sha256': digest(Path(__file__))}
    (args.report / 'audit.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
