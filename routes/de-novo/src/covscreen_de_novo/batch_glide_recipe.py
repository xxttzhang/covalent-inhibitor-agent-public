"""Fixed independent Glide SP recipe matching the tested De novo benchmark."""
import math
from pathlib import Path


def token(value):
    text = str(value)
    if not text or any(character.isspace() for character in text) or any(c in text for c in '\x00"\''):
        raise ValueError('Glide recipe paths must be unambiguous single tokens')
    return text


def grid_input(receptor, center):
    if len(center) != 3 or any(type(x) not in (int, float) or not math.isfinite(x) for x in center):
        raise ValueError('Grid center must have three finite coordinates')
    return ('RECEP_FILE ' + token(receptor) + '\nGRIDFILE prereaction_grid.zip\nGRID_CENTER '
        + ', '.join(str(float(x)) for x in center)
        + '\nINNERBOX 10, 10, 10\nOUTERBOX 30, 30, 30\nFORCEFIELD OPLS_2005\n')


def docking_input(grid, ligands):
    return ('GRIDFILE ' + token(grid) + '\nLIGANDFILE ' + token(ligands)
        + '\nPRECISION SP\nDOCKING_METHOD confgen\nCANONICALIZE True\nCORE_RESTRAIN False\n'
          'FORCEFIELD OPLS_2005\nPOSES_PER_LIG 20\nPOSTDOCK_NPOSE 20\nWRITE_CSV True\n')


def commands(vendor, control, *, cpu_count, input_count):
    if type(cpu_count) is not int or not 1 <= cpu_count <= 4 or type(input_count) is not int or not 2 <= input_count <= 7:
        raise ValueError('Prereaction recipe requires bounded CPU and candidate/control counts')
    vendor = Path(vendor)
    if not vendor.is_absolute():
        raise ValueError('Registered vendor runtime must be absolute')
    common = ['-HOST', f'localhost:{cpu_count}', '-NJOBS', str(min(cpu_count, input_count)),
              '-NOJOBID', '-WAIT', '-LOCAL', '-max_retries', '0']
    return {
        'control_ligprep': [str(vendor / 'ligprep'), '-isd', str(control), '-omae', 'control_states.mae',
            '-epik', '-ph', '7.0', '-pht', '1.0', '-s', '1', '-bff', '14', '-NJOBS', '1', '-WAIT', '-LOCAL'],
        'grid': [str(vendor / 'glide'), 'prereaction_grid.in', '-HOST', 'localhost:1', '-NJOBS', '1',
            '-NOJOBID', '-WAIT', '-LOCAL', '-max_retries', '0'],
        'dock': [str(vendor / 'glide'), 'independent_prereaction.in', *common,
            '-no_cleanup', '-strict', '-noforce'],
    }
