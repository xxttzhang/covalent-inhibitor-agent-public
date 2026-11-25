from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from covscreen.research import ProjectSeed, create_project, validate_project


SEARCHED_ON = "2026-07-31"


def citation(
    evidence_id: str,
    source_type: str,
    title: str,
    url: str,
    supports: list[str],
    *,
    doi: str | None = None,
    pmid: str | None = None,
) -> dict:
    item = {
        "id": evidence_id,
        "source_type": source_type,
        "title": title,
        "url": url,
        "retrieved_on": SEARCHED_ON,
        "supports": supports,
    }
    if doi:
        item["doi"] = doi
    if pmid:
        item["pmid"] = pmid
    return item


def criterion(
    criterion_id: str,
    status: str,
    weight: float,
    rationale: str,
    evidence_ids: list[str],
) -> dict:
    return {
        "id": criterion_id,
        "status": status,
        "weight": weight,
        "rationale": rationale,
        "evidence_ids": evidence_ids,
    }


def protocol(*queries: str) -> dict:
    return {
        "searched_on": SEARCHED_ON,
        "databases": ["UniProtKB", "RCSB PDB", "PubMed / primary journal record"],
        "queries": list(queries),
        "inclusion_rule": (
            "Include reviewed sequence records, experimentally determined structures, "
            "and primary studies that directly support or challenge target, residue, "
            "binding-site, or covalent-mechanism claims."
        ),
        "exclusion_rule": (
            "Exclude unsourced summaries and do not upgrade docking, generative output, "
            "or database annotation into experimental target-engagement evidence."
        ),
        "limitations": [
            "This targeted search is an evidence passport, not a systematic review.",
            "Assay-, construct-, nucleotide-, and mutation-state transferability remains to be tested.",
        ],
    }


SEEDS = [
    {
        "project": {
            "project_id": "egfr-c797",
            "title": "EGFR Cys797 targeted-covalent feasibility",
            "objective": (
                "Build an evidence-grounded starting package for selective EGFR Cys797 "
                "covalent-inhibitor design."
            ),
            "status": "evidence_ready",
            "benchmark_links": ["benchmark/frozen/BTK-mini-v1/manifest.json"],
        },
        "target_passport": {
            "target_id": "uniprot:P00533",
            "symbol": "EGFR",
            "name": "Epidermal growth factor receptor",
            "organism": "Homo sapiens",
            "uniprot_id": "P00533",
            "disease_context": (
                "EGFR-driven non-small-cell lung cancer; the exact intended mutation and "
                "line-of-therapy context must be selected before optimization."
            ),
            "target_hypothesis": (
                "Selective reversible recognition in the EGFR kinase pocket can position "
                "a controlled electrophile for Cys797 engagement."
            ),
            "site": {
                "site_id": "EGFR_C797",
                "chain_id": "A",
                "residue_name": "CYS",
                "residue_number": 797,
                "reactive_atom": "SG",
                "nucleophile_present": True,
                "reaction_hypothesis": "Cysteine thiol addition to a tuned Michael acceptor.",
            },
            "structures": ["PDB:4G5J"],
            "citations": [
                citation(
                    "egfr-uniprot-p00533",
                    "uniprot",
                    "UniProtKB P00533: Epidermal growth factor receptor",
                    "https://www.uniprot.org/uniprotkb/P00533/entry",
                    ["Target identity, organism, canonical sequence context."],
                ),
                citation(
                    "egfr-pdb-4g5j",
                    "pdb",
                    "PDB 4G5J: EGFR kinase in complex with afatinib (BIBW2992)",
                    "https://www.rcsb.org/structure/4G5J",
                    ["Experimental covalent complex and kinase-pocket geometry."],
                    doi="10.2210/pdb4G5J/pdb",
                ),
                citation(
                    "egfr-solca-2012",
                    "publication",
                    (
                        "Target binding properties and cellular activity of afatinib "
                        "(BIBW 2992), an irreversible ErbB family blocker"
                    ),
                    (
                        "https://jpet.aspetjournals.org/article/"
                        "S0022-3565%2824%2918396-6/abstract"
                    ),
                    [
                        "Primary crystallographic and mass-spectrometric evidence for "
                        "afatinib covalent engagement of ErbB-family kinases."
                    ],
                    doi="10.1124/jpet.112.197756",
                ),
            ],
            "known_unknowns": [
                "The preferred EGFR mutation/construct and disease segment are not yet fixed.",
                "Proteome-wide selectivity and intrinsic warhead reactivity are scaffold-dependent.",
                "A reference covalent complex does not establish performance of a new chemical series.",
            ],
        },
        "assessment_criteria": [
            criterion("biological_relevance", "supported", 2, "EGFR is a disease-relevant kinase, but the exact patient segment must be fixed.", ["egfr-uniprot-p00533", "egfr-solca-2012"]),
            criterion("site_identity", "supported", 2, "Cys797 is the nominated kinase-site nucleophile.", ["egfr-pdb-4g5j", "egfr-solca-2012"]),
            criterion("structural_access", "supported", 2, "4G5J directly observes a ligand-bound kinase pocket.", ["egfr-pdb-4g5j"]),
            criterion("ligandability", "supported", 1, "A ligand-bound experimental kinase structure supplies recognition precedent.", ["egfr-pdb-4g5j"]),
            criterion("covalent_precedent", "supported", 1, "Afatinib supplies direct covalent precedent.", ["egfr-pdb-4g5j", "egfr-solca-2012"]),
            criterion("reaction_plausibility", "supported", 1, "The primary study supports an acrylamide-based targeted covalent mechanism.", ["egfr-solca-2012"]),
            criterion("selectivity_window", "unknown", 1, "A new series needs matched reversible controls and proteome-wide selectivity measurements.", []),
        ],
        "assets": {
            "known_ligands": True,
            "reusable_scaffold": True,
            "reference_structure": True,
            "notes": ["No project-local screening library or prepared receptor has been declared."],
        },
        "search_protocol": protocol(
            "EGFR P00533 Cys797 covalent inhibitor",
            "EGFR afatinib 4G5J covalent structure",
            "EGFR Cys797 primary mass spectrometry afatinib",
        ),
    },
    {
        "project": {
            "project_id": "kras-g12c",
            "title": "KRAS G12C mutant-selective covalent feasibility",
            "objective": (
                "Build an evidence-grounded starting package for mutant-specific "
                "KRAS G12C covalent-inhibitor design."
            ),
            "status": "evidence_ready",
            "benchmark_links": ["benchmark/frozen/BTK-mini-v1/manifest.json"],
        },
        "target_passport": {
            "target_id": "uniprot:P01116|variant:G12C",
            "symbol": "KRAS G12C",
            "name": "GTPase KRas, Gly12Cys variant",
            "organism": "Homo sapiens",
            "uniprot_id": "P01116",
            "disease_context": (
                "KRAS G12C-mutant cancer; tumour type, allele fraction, co-mutations, "
                "and nucleotide-state biology remain project-selection variables."
            ),
            "target_hypothesis": (
                "The mutant cysteine can support allele-selective covalent engagement "
                "from the switch-II pocket."
            ),
            "site": {
                "site_id": "KRAS_G12C",
                "chain_id": "A",
                "residue_name": "CYS",
                "residue_number": 12,
                "reactive_atom": "SG",
                "nucleophile_present": True,
                "reaction_hypothesis": (
                    "Mutant Cys12 thiol addition to an electrophile positioned by "
                    "switch-II-pocket recognition."
                ),
            },
            "structures": ["PDB:4LYH"],
            "citations": [
                citation(
                    "kras-uniprot-p01116",
                    "uniprot",
                    "UniProtKB P01116: GTPase KRas",
                    "https://www.uniprot.org/uniprotkb/P01116/entry",
                    ["Canonical KRAS target identity and wild-type Gly12 sequence context."],
                ),
                citation(
                    "kras-pdb-4lyh",
                    "pdb",
                    "PDB 4LYH: vinylsulfonamide 9 covalently bound to KRAS G12C",
                    "https://www.rcsb.org/structure/4LYH",
                    ["Experimental G12C covalent complex and switch-II-pocket geometry."],
                    doi="10.2210/pdb4LYH/pdb",
                ),
                citation(
                    "kras-ostrem-2013",
                    "publication",
                    (
                        "K-Ras(G12C) inhibitors allosterically control GTP affinity "
                        "and effector interactions"
                    ),
                    "https://pubmed.ncbi.nlm.nih.gov/24256730/",
                    [
                        "Primary biochemical and structural evidence for mutant-cysteine-dependent "
                        "covalent inhibition and lack of wild-type engagement."
                    ],
                    doi="10.1038/nature12796",
                    pmid="24256730",
                ),
            ],
            "known_unknowns": [
                "Switch-II-pocket state and nucleotide-state occupancy can alter apparent tractability.",
                "New chemistry needs explicit wild-type and cysteine-dependence controls.",
                "Clinical activity cannot be inferred from a structural feasibility package.",
            ],
        },
        "assessment_criteria": [
            criterion("biological_relevance", "supported", 2, "The G12C molecular context is disease-relevant, while indication-specific choices remain.", ["kras-uniprot-p01116", "kras-ostrem-2013"]),
            criterion("site_identity", "supported", 2, "The project explicitly targets the Gly12Cys variant, not canonical wild-type KRAS.", ["kras-uniprot-p01116", "kras-pdb-4lyh"]),
            criterion("structural_access", "supported", 2, "4LYH directly observes the mutant covalent complex.", ["kras-pdb-4lyh"]),
            criterion("ligandability", "supported", 1, "The primary study identifies a ligandable switch-II pocket.", ["kras-pdb-4lyh", "kras-ostrem-2013"]),
            criterion("covalent_precedent", "supported", 1, "The mutant cysteine has direct structural and biochemical covalent precedent.", ["kras-pdb-4lyh", "kras-ostrem-2013"]),
            criterion("reaction_plausibility", "supported", 1, "A structure-supported electrophile-to-Cys12 reaction is reported.", ["kras-pdb-4lyh"]),
            criterion("selectivity_window", "supported", 1, "The primary study reports dependence on mutant Cys12 and no wild-type effect for the reported series; new series still need controls.", ["kras-ostrem-2013"]),
        ],
        "assets": {
            "known_ligands": True,
            "reusable_scaffold": True,
            "reference_structure": True,
            "notes": ["No project-local screening library or prepared receptor has been declared."],
        },
        "search_protocol": protocol(
            "KRAS P01116 Gly12 sequence",
            "KRAS G12C 4LYH covalent switch-II pocket",
            "Ostrem 2013 KRAS G12C mutant specific covalent inhibitor",
        ),
    },
    {
        "project": {
            "project_id": "kras-wt-g12-control",
            "title": "Wild-type KRAS Gly12 invalid-site control",
            "objective": (
                "Verify that the workflow stops a G12C-style covalent campaign when "
                "the stated molecular context is wild-type KRAS."
            ),
            "status": "evidence_ready",
            "benchmark_links": ["benchmark/frozen/BTK-mini-v1/manifest.json"],
        },
        "target_passport": {
            "target_id": "uniprot:P01116|wild-type",
            "symbol": "KRAS WT",
            "name": "Wild-type GTPase KRas",
            "organism": "Homo sapiens",
            "uniprot_id": "P01116",
            "disease_context": (
                "Negative molecular-context control; this is not a therapeutic campaign."
            ),
            "target_hypothesis": (
                "A G12C-directed reaction hypothesis must be rejected because wild-type "
                "KRAS contains Gly, not Cys, at position 12."
            ),
            "site": {
                "site_id": "KRAS_WT_G12",
                "chain_id": "A",
                "residue_name": "GLY",
                "residue_number": 12,
                "reactive_atom": None,
                "nucleophile_present": False,
                "reaction_hypothesis": "No Cys12 covalent reaction is chemically available in wild-type sequence.",
            },
            "structures": ["PDB:5W22"],
            "citations": [
                citation(
                    "kras-wt-uniprot-p01116",
                    "uniprot",
                    "UniProtKB P01116: GTPase KRas canonical sequence",
                    "https://www.uniprot.org/uniprotkb/P01116-1/entry",
                    ["The canonical sequence begins MTEYKLVVVGAGGV..., placing Gly at residue 12."],
                ),
                citation(
                    "kras-wt-pdb-5w22",
                    "pdb",
                    "PDB 5W22: human KRAS reference structure record",
                    "https://www.rcsb.org/structure/5W22",
                    ["Experimentally determined KRAS structural context; use entity/mutation annotations when selecting chains."],
                    doi="10.2210/pdb5W22/pdb",
                ),
                citation(
                    "kras-wt-ostrem-2013",
                    "publication",
                    (
                        "K-Ras(G12C) inhibitors allosterically control GTP affinity "
                        "and effector interactions"
                    ),
                    "https://pubmed.ncbi.nlm.nih.gov/24256730/",
                    ["The reported compounds rely on mutant Cys12 and do not affect wild-type protein."],
                    doi="10.1038/nature12796",
                    pmid="24256730",
                ),
            ],
            "known_unknowns": [
                "This control does not assess other potentially ligandable or covalent sites on KRAS.",
                "PDB construct mutations and chain annotations must be checked before reusing a structure.",
            ],
        },
        "assessment_criteria": [
            criterion("biological_relevance", "unknown", 2, "No therapeutic disease hypothesis is asserted for this negative-control package.", []),
            criterion("site_identity", "challenged", 2, "Wild-type residue 12 is Gly and cannot be treated as mutant Cys12.", ["kras-wt-uniprot-p01116", "kras-wt-ostrem-2013"]),
            criterion("structural_access", "supported", 2, "A KRAS structure exists, but it does not repair the incorrect residue identity.", ["kras-wt-pdb-5w22"]),
            criterion("ligandability", "unknown", 1, "General KRAS ligandability is irrelevant to the invalid Cys12 claim.", []),
            criterion("covalent_precedent", "challenged", 1, "G12C precedent is mutation-specific and does not transfer to wild type.", ["kras-wt-ostrem-2013"]),
            criterion("reaction_plausibility", "challenged", 1, "Gly12 has no cysteine thiol for the stated reaction.", ["kras-wt-uniprot-p01116"]),
            criterion("selectivity_window", "unknown", 1, "No compound series should be evaluated until the molecular context is corrected.", []),
        ],
        "assets": {
            "reference_structure": True,
            "notes": ["Negative control: assets must not override a critical target/site mismatch."],
        },
        "search_protocol": protocol(
            "KRAS P01116 canonical residue 12",
            "KRAS G12C inhibitors wild-type specificity",
            "human KRAS reference structure residue 12",
        ),
    },
]


def main() -> None:
    projects_root = ROOT / "projects"
    results = []
    for payload in SEEDS:
        seed = ProjectSeed.model_validate(payload)
        project_dir = projects_root / seed.project.project_id
        if project_dir.exists():
            results.append(
                {
                    "project_id": seed.project.project_id,
                    "status": "SKIPPED",
                    "reason": "project already exists",
                }
            )
            continue
        create_project(seed, projects_root)
        results.append(validate_project(project_dir))
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
