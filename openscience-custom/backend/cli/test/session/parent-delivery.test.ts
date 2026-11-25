import { expect, test } from "bun:test"
import { publishParentDelivery } from "../../src/session/parent-delivery"
import { ArtifactStore } from "../../src/artifact/store"

function fixture() {
  const evidence = "hyp-" + "a".repeat(24)
  const workflow = "ligand-flow-" + "b".repeat(24)
  const files = [
    ["hypothesis.json", "application/json", JSON.stringify({ evidence_id: evidence, hypothesis: { route: "ligand-first" } })],
    ["report.md", "text/markdown", "# 正式报告\nNo compute submitted.\n"],
  ].map(([name, mime_type, text]) => ({ name, mime_type, bytes: Buffer.byteLength(text),
    sha256: new Bun.CryptoHasher("sha256").update(text).digest("hex"), base64: Buffer.from(text).toString("base64") }))
  return { workflow, scope: { project: `prj_${crypto.randomUUID()}`, session: "ses_delivery_test" },
    envelope: { structuredContent: { result: { schema: "ligand-parent-delivery-v1", workflow_id: workflow,
      evidence_id: evidence, proposal_id: "proposal-" + "c".repeat(12), proposal_sha256: files[0].sha256,
      hash_scope: "captured bytes", compute_submitted: false, files } } } }
}

test("official bytes survive Results storage and repeated export reuses versions", async () => {
  const value = fixture()
  await publishParentDelivery(value.envelope, value.scope, value.workflow)
  await publishParentDelivery(value.envelope, value.scope, value.workflow)
  const artifacts = await ArtifactStore.list(value.scope.project)
  expect(artifacts).toHaveLength(2)
  for (const artifact of artifacts) {
    expect(artifact.versionCount).toBe(1)
    const stored = await ArtifactStore.read(value.scope.project, artifact.id)
    const source = value.envelope.structuredContent.result.files.find(file => file.name === stored!.info.filename)!
    expect(Buffer.from(await stored!.content.arrayBuffer()).toString("base64")).toBe(source.base64)
    expect(await ArtifactStore.read("another-project", artifact.id)).toBeUndefined()
  }
})

test.each(["hash", "workflow", "duplicate", "mime", "proposal"])("reject %s before publishing any file", async (kind) => {
  const value = fixture()
  const delivery = value.envelope.structuredContent.result
  if (kind === "hash") delivery.files[1].sha256 = "0".repeat(64)
  if (kind === "workflow") delivery.workflow_id = "ligand-flow-" + "d".repeat(24)
  if (kind === "duplicate") delivery.files[1] = delivery.files[0]
  if (kind === "mime") delivery.files[1].mime_type = "application/json"
  if (kind === "proposal") delivery.proposal_sha256 = "0".repeat(64)
  await expect(publishParentDelivery(value.envelope, value.scope, value.workflow)).rejects.toThrow()
  expect(await ArtifactStore.list(value.scope.project)).toHaveLength(0)
})

function designFixture() {
  const value = fixture()
  const identity = "design-" + "d".repeat(24)
  const source = value.envelope.structuredContent.result
  const { proposal_id, proposal_sha256, ...base } = source
  const content = JSON.stringify({ design_id: identity, evidence_id: base.evidence_id })
  return { ...value, envelope: { structuredContent: { result: { ...base,
    schema: "ligand-design-delivery-v1", design_id: identity, delivery_manifest_sha256: "e".repeat(64),
    files: [{ name: "disposition.json", mime_type: "application/json", bytes: Buffer.byteLength(content),
      sha256: new Bun.CryptoHasher("sha256").update(content).digest("hex"), base64: Buffer.from(content).toString("base64") }, base.files[1]],
  } } } }
}

test("design originals publish with separate identity and idempotent immutable versions", async () => {
  const value = designFixture()
  const result = await publishParentDelivery(value.envelope, value.scope, value.workflow)
  expect(JSON.stringify(result)).toContain("DESIGN_REPORT_DELIVERED")
  expect(JSON.stringify(result)).not.toContain('"base64"')
  await publishParentDelivery(value.envelope, value.scope, value.workflow)
  const artifacts = await ArtifactStore.list(value.scope.project)
  expect(artifacts).toHaveLength(2)
  for (const artifact of artifacts) {
    expect(artifact.versionCount).toBe(1)
    expect(artifact.current.sourcePath).toContain(value.envelope.structuredContent.result.design_id)
    const saved = await ArtifactStore.read(value.scope.project, artifact.id)
    const file = value.envelope.structuredContent.result.files.find(x => x.name === saved!.info.filename)!
    expect(Buffer.from(await saved!.content.arrayBuffer()).toString("base64")).toBe(file.base64)
  }
})

test("design identity mismatch fails before any Results writes", async () => {
  const value = designFixture()
  value.envelope.structuredContent.result.design_id = "design-" + "f".repeat(24)
  await expect(publishParentDelivery(value.envelope, value.scope, value.workflow)).rejects.toThrow()
  expect(await ArtifactStore.list(value.scope.project)).toHaveLength(0)
})

function covdockFixture() {
  const value = fixture()
  const design = "design-" + "d".repeat(24)
  const report = { schema: "ligand-covdock-report-v1", workflow_id: value.workflow,
    evidence_id: value.envelope.structuredContent.result.evidence_id, design_id: design,
    compute_submitted_by_report: false }
  const files = [["report.json", "application/json", JSON.stringify(report)],
    ["report.md", "text/markdown", "# LO report\nAF3 optional; no MD.\n"]].map(([name, mime_type, text]) =>
    ({ name, mime_type, bytes: Buffer.byteLength(text), sha256: new Bun.CryptoHasher("sha256").update(text).digest("hex"),
      base64: Buffer.from(text).toString("base64") }))
  return { ...value, envelope: { structuredContent: { result: {
    schema: "ligand-covdock-delivery-v1", workflow_id: value.workflow, evidence_id: report.evidence_id,
    design_id: design, report_id: "ligand-covdock-report-" + "e".repeat(24),
    delivery_manifest_sha256: "f".repeat(64), hash_scope: "sealed report", compute_submitted: false, files } } } }
}

test("LO report exports exact bytes to Results without requiring graph delivery", async () => {
  const value = covdockFixture()
  const result = await publishParentDelivery(value.envelope, value.scope, value.workflow)
  expect(JSON.stringify(result)).toContain("COVDOCK_REPORT_DELIVERED")
  await publishParentDelivery(value.envelope, value.scope, value.workflow)
  const artifacts = await ArtifactStore.list(value.scope.project)
  expect(artifacts).toHaveLength(2)
  for (const artifact of artifacts) {
    expect(artifact.versionCount).toBe(1)
    const stored = await ArtifactStore.read(value.scope.project, artifact.id)
    const file = value.envelope.structuredContent.result.files.find(f => f.name === stored!.info.filename)!
    expect(Buffer.from(await stored!.content.arrayBuffer()).toString("base64")).toBe(file.base64)
  }
})

test.each(["design_id", "evidence_id"] as const)("LO report rejects mismatched %s before saving", async key => {
  const value = covdockFixture()
  value.envelope.structuredContent.result[key] = (key === "design_id" ? "design-" : "hyp-") + "0".repeat(24)
  await expect(publishParentDelivery(value.envelope, value.scope, value.workflow)).rejects.toThrow()
  expect(await ArtifactStore.list(value.scope.project)).toHaveLength(0)
})
