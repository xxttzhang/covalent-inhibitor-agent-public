"""Installation-owned Ligand executor selection; never a model tool argument."""
import os


def selected_instance():
    # Preserve historical installations; the new isolated deployment sets asgbie4.
    value = os.environ.get('COVALENT_LIGAND_VS_INSTANCE', '') or 'asgbie3'
    if value not in {'asgbie3', 'asgbie4'}:
        raise ValueError('Unregistered Ligand CPU execution instance')
    return value
