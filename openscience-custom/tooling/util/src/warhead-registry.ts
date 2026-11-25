import { z } from "zod"

export const warheadID = z.enum(["acrylamide", "cyanamide", "nitrile", "methyl-acrylate"])
/** Substructure recognition only; not executable reaction templates or calibrated geometry gates. */
export const warheadRegistry = {
  schema: "covalent-warhead-registry-v1",
  evidence_scope: "Literature examples support chemistry classes, not a new target/site pair",
  entries: [
    { id: "acrylamide", family: "michael-acceptor-amide", smarts: "[C;X3:1]=[C;X3;!$(C-C#N):2]-[C;X3:3](=[O:4])-[N:5]", reactive_map: 1, motif: "C=C-C(=O)-N",
      electrophile: "beta carbon of the conjugated alkene", nucleophile: "cysteine sulfur when supported by site evidence",
      product: "C-S bond at beta carbon; alkene becomes single; protonation must be mapped",
      mechanism: "Michael addition", reversibility: "usually designed as irreversible; candidate-specific evidence required",
      exclusions: ["cyanoacrylamide", "acrylate ester", "special substituted mechanisms require separate profiles"],
      concerns: ["installation vector", "thiol reactivity", "off-target engagement"],
      references: ["https://pmc.ncbi.nlm.nih.gov/articles/PMC8577418/"] },
    { id: "cyanamide", family: "N-linked-cyano", smarts: "[N;X3:1]-[C;X2:2]#[N;X1:3]", reactive_map: 2, motif: "N-C#N",
      electrophile: "cyano carbon directly attached to nitrogen", nucleophile: "evidence-supported cysteine sulfur",
      product: "isothiourea-type adduct; charge, protonation and tautomers require a dedicated template",
      mechanism: "addition to cyano carbon", reversibility: "reversible covalent hypothesis; not established by the motif alone",
      exclusions: ["ordinary carbon-linked nitrile", "cyanoacrylamide"],
      concerns: ["substitution on nitrogen", "proton transfer", "adduct and plasma stability"],
      references: ["https://pmc.ncbi.nlm.nih.gov/articles/PMC4018094/"] },
    { id: "nitrile", family: "carbon-linked-cyano", smarts: "[#6;!$(C=C-C(=O)[N,O]);!$(C(=C)-C(=O)[N,O]):1]-[C;X2:2]#[N;X1:3]", reactive_map: 2, motif: "C-C#N",
      electrophile: "nitrile carbon", nucleophile: "site-specific evidence required; cysteine is the initial platform scope",
      product: "thioimidate-type cysteine adduct; protonation and tautomer mapping required",
      mechanism: "addition to nitrile carbon", reversibility: "reversible covalent hypothesis; an arbitrary cyano group is not a validated warhead",
      exclusions: ["cyanamide", "cyanoacrylamide", "nitrile used only as a nonreactive substituent"],
      concerns: ["local catalytic environment", "orientation", "proton transfer"],
      references: ["https://pubmed.ncbi.nlm.nih.gov/34726479/"] },
    { id: "methyl-acrylate", family: "michael-acceptor-ester", smarts: "[C;X3:1]=[C;X3;!$(C-C#N):2]-[C;X3:3](=[O:4])-[O;X2:5]-[C;H3:6]", reactive_map: 1, motif: "C=C-C(=O)-O-CH3",
      electrophile: "beta carbon of the conjugated alkene", nucleophile: "evidence-supported cysteine sulfur",
      product: "C-S bond at beta carbon; alkene becomes single; ester remains unless independently transformed",
      mechanism: "Michael addition", reversibility: "candidate-specific; not inferred from acrylamide defaults",
      exclusions: ["methyl cyanoacrylate", "activated ester acyl substitution"],
      concerns: ["ester hydrolysis", "thiol reactivity", "attachment vector", "selectivity"],
      references: ["https://pmc.ncbi.nlm.nih.gov/articles/PMC5880509/"] },
  ],
  policy: { default_reactive_warheads_per_candidate: 1, cross_warhead_raw_score_ranking: false,
    shared_attack_angle_threshold: false, conventional_md_proves_reaction_or_reversibility: false,
    executable_reaction_templates_for_new_classes: false },
} as const

export function warheadDefinition(id: string) {
  return warheadRegistry.entries.find(entry => entry.id === id) ?? null
}
