import { studyRequest } from "@synsci/util/discovery-study"

/** Engineering fixture; no dependency on private historical research data. */
export function studyFixture() {
  return studyRequest.parse({ title: "Study lifecycle fixture",
    target: { id: "fixture", gene: "EGFR", uniprot: "P00533", taxon: 9606,
      site: { chain: "A", number: 797, residue: "CYS", atom: "SG", numbering: "canonical" },
      receptor: { reference: "fixture.pdb" } },
    hypothesis: { state: "pending", rationale: "", evidence: [] },
    routes: { "library-first": { selected: true, mode: "new", inputs: {} },
      "ligand-first": { selected: false, mode: "new", inputs: {} },
      "de-novo": { selected: false, mode: "new", inputs: {} } },
  })
}
