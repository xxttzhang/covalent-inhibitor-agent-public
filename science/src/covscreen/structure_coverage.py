"""Report observed PDB polymer coordinates without inventing sequence coverage."""
import math
from pathlib import Path

from .jobs import sha256_file


def observed_coverage(pdb: Path, structure: dict) -> dict:
    pdb = Path(pdb)
    if pdb.is_symlink() or not pdb.is_file() or sha256_file(pdb) != structure['input_sha256']:
        raise ValueError('Coverage receptor missing or differs from frozen structure')
    selected_model = int(structure['model'])
    selected_chain = structure['author_chain']
    current_model = 1
    residues = {}
    for line in pdb.read_text().splitlines():
        record = line[:6].strip()
        if record == 'MODEL':
            current_model = int(line[10:14])
        elif record == 'ENDMDL':
            current_model = None
        elif record == 'ATOM' and current_model == selected_model and line[21:22].strip() == selected_chain:
            position = int(line[22:26])
            insertion = line[26:27].strip()
            xyz = [float(line[a:b]) for a, b in ((30, 38), (38, 46), (46, 54))]
            if not all(math.isfinite(v) for v in xyz):
                raise ValueError('Nonfinite coordinates in coverage receptor')
            key = (position, insertion)
            name = line[17:20].strip()
            if key in residues and residues[key] != name:
                raise ValueError('Ambiguous residue identity in coverage receptor')
            residues[key] = name
    if not residues:
        raise ValueError('No observed polymer ATOM residues in selected model and chain')
    # Integer ranges deliberately exclude insertion-coded residues; never silently
    # merge 100A with 100 or label a numbering discontinuity a missing residue.
    positions = sorted(number for number, insertion in residues if not insertion)
    ranges = []
    for number in positions:
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    return {
        'schema': 'observed-structure-coverage-v1',
        'source_sha256': structure['input_sha256'],
        'model': selected_model, 'author_chain': selected_chain,
        'observed_polymer_residue_count': len(residues),
        'observed_author_ranges': ranges,
        'insertion_coded_residues': [f'{n}{i}' for n, i in sorted(residues) if i],
        'canonical_coverage_status': 'UNKNOWN_REQUIRES_VERIFIED_FULL_RESIDUE_MAPPING',
        'full_length_coverage_verified': False,
        'limitations': [
            'Ranges use author numbering and count residues with at least one ATOM coordinate, not complete residues.',
            'Author numbering discontinuities do not by themselves prove missing canonical residues.',
            'HETATM records are excluded; modified polymer residues require a verified polymer mapping.',
            'Do not infer full-length coverage, canonical ranges, or coverage percentage from these author ranges or a single selected site.',
        ],
    }


def mapped_coordinate_coverage(residue_map, canonical_map, canonical_length):
    """Summarize an already verified residue map; never extrapolate an offset."""
    if type(canonical_length) is not int or canonical_length < 1:
        raise ValueError('Verified canonical sequence length is required')
    positions = set()
    unmapped = []
    for row in residue_map.values():
        author = str(row['author_residue_number'])
        insertion = row['insertion_code'] or ''
        position = None if insertion else canonical_map.get(author)
        if position is None:
            unmapped.append(author + insertion)
            continue
        if type(position) is not int or not 1 <= position <= canonical_length:
            raise ValueError('Canonical mapping falls outside verified target sequence')
        if position in positions:
            raise ValueError('Multiple observed residues map to one canonical position')
        positions.add(position)
    def ranges(values):
        result = []
        for value in sorted(values):
            if result and value == result[-1][1] + 1:
                result[-1][1] = value
            else:
                result.append([value, value])
        return result
    covered = ranges(positions)
    missing = ranges(set(range(1, canonical_length + 1)) - positions)
    return {'schema': 'mapped-coordinate-coverage-v1', 'canonical_sequence_length': canonical_length,
        'observed_residue_count': len(residue_map), 'mapped_observed_residue_count': len(positions),
        'observed_canonical_ranges': covered, 'canonical_positions_without_verified_coordinates': missing,
        'unmapped_observed_author_residues': unmapped,
        'canonical_coordinate_fraction': len(positions) / canonical_length,
        'all_canonical_positions_have_coordinates': len(positions) == canonical_length,
        'mapping_complete_for_observed_residues': not unmapped,
        'limitations': [
            'Coverage refers to residues with observed ATOM coordinates in the selected model and chain, not complete side chains.',
            'Positions without verified coordinates may be outside the construct or unresolved; this report does not distinguish those causes.',
            'Insertion-coded residues are not assigned canonical positions by numerical offset.',
            'Coverage does not establish full-length functional conformation, pocket accessibility or ligand activity.',
        ]}
