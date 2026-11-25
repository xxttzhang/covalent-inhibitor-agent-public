"""Bounded read-only coordinates/chemistry of one fixed candidate or control pose."""
import json
import math
import re
import sys

from extract_covdock_pose_coordinates import extract_pose
from inspect_covdock_output import readback_request, _atom_index, _text, _integer, _bonded_atoms


def atom_record(atom):
    return {'atom_index': _atom_index(atom), 'atom_name': _text(atom, 'pdbname', 'atom_name', 'name').upper(),
        'chain': _text(atom, 'chain'), 'resnum': _integer(atom, 'resnum', 'residue_number'),
        'icode': _text(atom, 'inscode'), 'resname': _text(atom, 'pdbres', 'resname').upper(),
        'atomic_number': int(atom.atomic_number), 'formal_charge': int(atom.formal_charge),
        'isotope': int(atom.isotope), 'hydrogen_count': sum(int(a.atomic_number) == 1 for a in _bonded_atoms(atom))}


def read_control(request):
    if (not isinstance(request, dict) or set(request) != {
            'version', 'files', 'attachment_residue', 'pose_index', 'title', 'score_property', 'score'}
            or request['version'] not in {'covdock-control-coordinates-v1', 'covdock-candidate-chemistry-v1'}
            or type(request['pose_index']) is not int or not 0 <= request['pose_index'] < 1000
            or not isinstance(request['title'], str) or len(request['title']) > 256
            or request['score_property'] not in {'r_cdock_cdock_affinity', 'r_i_docking_score',
                'r_i_sample_docking_score', 'r_i_sample_glide_gscore'}
            or type(request['score']) not in (int, float) or not math.isfinite(request['score'])):
        raise ValueError('Invalid control readback request')
    candidate = request['version'] == 'covdock-candidate-chemistry-v1'
    identity = r'candidate_000__p[A-Za-z0-9._-]+__i0' if candidate else r'control_afatinib__p[A-Za-z0-9._-]+__i1'
    if not re.fullmatch(identity, request['title']):
        raise ValueError('Readback title differs from its fixed candidate/control role')
    readback_request({key: ('covdock-structure-readback-v1' if key == 'version' else request[key])
                      for key in ('version', 'files', 'attachment_residue')})
    if len(request['files']) != 1 or request['attachment_residue'] != 'A:797':
        raise ValueError('Control reader requires one EGFR C797 structure container')
    from schrodinger.structure import StructureReader
    result, count = None, 0
    with StructureReader(request['files'][0]) as reader:
        for index, structure in enumerate(reader):
            count += 1
            if count > 1000 or structure.atom_total > 50000:
                raise ValueError('Control structure exceeds readback bounds')
            if index != request['pose_index']:
                continue
            if (structure.title != request['title']
                    or structure.property.get(request['score_property']) != request['score']):
                raise ValueError('Selected control title or score changed')
            result = extract_pose(structure, index, attachment_chain='A',
                                  attachment_residue=797, nucleophilic_atom='SG')
            atoms = {_atom_index(atom): atom for atom in structure.atom}
            for atom in result['ligand_heavy_atoms']:
                original = atoms[atom['atom_index']]
                atom['formal_charge'] = int(original.formal_charge)
                atom['isotope'] = int(original.isotope)
                atom['hydrogen_count'] = sum(int(a.atomic_number) == 1 for a in _bonded_atoms(original))
                atom['hydrogen_isotopes'] = sorted(int(a.isotope) for a in _bonded_atoms(original)
                                                  if int(a.atomic_number) == 1)
            if candidate:
                # Enumerate every heavy-atom edge crossing the ligand boundary,
                # including unintended links to a second protein residue.
                ligand_indices = {row['atom_index'] for row in result['ligand_heavy_atoms']}
                external = []
                for bond in structure.bond:
                    first, second = bond.atom1, bond.atom2
                    if (int(first.atomic_number) <= 1 or int(second.atomic_number) <= 1
                            or (_atom_index(first) in ligand_indices) == (_atom_index(second) in ligand_indices)):
                        continue
                    if _atom_index(second) in ligand_indices: first, second = second, first
                    external.append({'ligand_atom_index':_atom_index(first),
                        'partner':atom_record(second), 'order':float(bond.order)})
                result['external_heavy_bonds'] = external
                targets = [a for a in atoms.values() if _text(a, 'chain') == 'A'
                    and _integer(a, 'resnum', 'residue_number') == 797
                    and _text(a, 'pdbname', 'atom_name', 'name').upper() == 'SG']
                if len(targets) != 1:
                    raise ValueError('Candidate nucleophile is ambiguous')
                target = targets[0]
                neighbors = []
                for bond in structure.bond:
                    if _atom_index(bond.atom1) == _atom_index(target): other = bond.atom2
                    elif _atom_index(bond.atom2) == _atom_index(target): other = bond.atom1
                    else: continue
                    neighbors.append({'atom':atom_record(other), 'order':float(bond.order)})
                result['nucleophile'] = {'atom':atom_record(target), 'bonds':neighbors}
            # Carry insertion codes explicitly; residue numbers alone are not identity.
            for row in result['protein_ca']:
                matches = [a for a in atoms.values() if _text(a, 'chain') == row['chain']
                    and _integer(a, 'resnum', 'residue_number') == row['resnum']
                    and _text(a, 'pdbname', 'atom_name', 'name').upper() == 'CA']
                if len(matches) != 1:
                    raise ValueError('Ambiguous receptor residue in control structure')
                row['icode'] = _text(matches[0], 'inscode')
    if result is None:
        raise ValueError('Selected control pose is missing')
    return {'version': request['version'], 'input': request['files'][0],
            'pose_count': count, 'pose': result, 'scientific_quality_verified': False}


if __name__ == '__main__':
    if sys.argv[1:] != ['--readback-stdin']:
        raise ValueError('Use the bounded native readback protocol')
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError('Control request exceeds bounds')
    print(json.dumps(read_control(json.loads(raw)), allow_nan=False))
