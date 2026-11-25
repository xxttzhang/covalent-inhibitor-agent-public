"""Canonical Ligand-first candidate table fields shared by route producers."""


def candidate_table_row(candidate, campaign, input_sdf_index):
    return {
        "compound_id": candidate.candidate_id,
        "parent_id": campaign.parent_pose.ligand_component_id,
        "protomer_id": candidate.candidate_id,
        "target_id": campaign.target.symbol,
        "site_id": f"{campaign.target.symbol}_C{campaign.target.nucleophile.uniprot_residue_number}",
        "label": "candidate",
        "standardized_smiles": candidate.canonical_smiles,
        "input_sdf_index": input_sdf_index,
        "warhead_class": candidate.warhead_name,
        "linker_name": candidate.linker_name,
        "linker_heavy_atom_count": candidate.linker_heavy_atom_count,
        "exit_vector_id": candidate.exit_vector_id,
        "transformation_name": candidate.transformation_name,
        "electrophilic_atom_map": candidate.quality_gate.reaction_atom_map_number,
        "reaction_type": candidate.reaction_class,
        "carsidock_reaction_type": {
            "acrylamide": "Michael Addition",
            "vinyl_sulfone": "Michael Addition",
            "chloroacetamide": "Nucleophilic Substitution",
            # Upstream mol_edit handles activated alkynes in the
            # Michael branch; the triple-bond branch is nitrile-only.
            "propiolamide": "Michael Addition",
        }[candidate.warhead_name],
        "covdock_reaction_type": {
            "acrylamide": "Michael Addition",
            "vinyl_sulfone": "Michael Addition",
            "chloroacetamide": "Nucleophilic Substitution",
            "propiolamide": "Conjugate Addition to Alkyne (carbonyl activated)",
        }[candidate.warhead_name],
        "chemistry_gate": candidate.quality_gate.status,
        "protected_core_retained": candidate.quality_gate.protected_core_retained,
        "protected_pharmacophore_retained": (
            candidate.quality_gate.protected_pharmacophore_retained
        ),
        # Chloroacetamide and vinyl sulfone are conservatively
        # flagged as higher-intrinsic-reactivity hypotheses. This
        # is a triage annotation, not a kinetic measurement.
        "high_reactivity_alert": candidate.warhead_name
        in {"chloroacetamide", "vinyl_sulfone"},
        "reactivity_annotation_method": "curated_warhead_class_v1",
    }
