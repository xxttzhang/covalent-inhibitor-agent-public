import { test, expect } from "bun:test"
import path from "node:path"
import { hypothesisDisplay, hypothesisDisplayReport, evidenceURL } from "@synsci/util/discovery-hypothesis-display"

test("real frozen hypothesis preserves claims, citations, numbering and uncertainties", async () => {
  const root = path.resolve(import.meta.dir, "../../../..")
  const receipt = await Bun.file(path.join(root, ".discovery/studies/study-c92778ff-f0a4-4743-bf95-ea403b8966ac/ligand-first.hypothesis.import.json")).json()
  const payload = JSON.parse(receipt.original)
  const view = hypothesisDisplay(receipt.original)
  const report = hypothesisDisplayReport(receipt.original).join("\n")
  expect(view.rationale).toBe(payload.hypothesis.rationale)
  expect(view.claims.map(item => item.statement)).toEqual(payload.hypothesis.claims.map((item: { statement: string }) => item.statement))
  expect(view.site.join("\n")).toContain("773 → 标准 797")
  for (const uncertainty of payload.hypothesis.uncertainties) expect(report).toContain(uncertainty)
  for (const reference of view.references) {
    expect(report).toContain(reference.title)
    if (reference.url) expect(report).toContain(reference.url)
  }
  expect(view.route).toBe("ligand-first")
})

test("missing evidence stays missing and unsafe links never become clickable", () => {
  for (const url of ["javascript:alert(1)", "file:///etc/passwd", "https://user:secret@example.com", "data:text/html,test", "not a URL"]) expect(evidenceURL(url)).toBeUndefined()
  expect(evidenceURL("https://pubmed.ncbi.nlm.nih.gov/27251290/")).toBe("https://pubmed.ncbi.nlm.nih.gov/27251290/")
  const original = JSON.stringify({ hypothesis: { claims: [{ statement: "Unverified", evidence_ids: ["absent"] }] } })
  const view = hypothesisDisplay(original)
  expect(view.claims[0].evidence[0].reference).toBeUndefined()
  expect(hypothesisDisplayReport(original).join("\n")).toContain("未提供参考文献记录")
  expect(view.rationale).toContain("未提供")
})
