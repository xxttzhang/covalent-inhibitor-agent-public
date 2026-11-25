import z from "zod"
import { ArtifactStore } from "../artifact/store"
import { Lock } from "../util/lock"

const digest = z.string().regex(/^[a-f0-9]{64}$/)
const parent = z.object({
  schema: z.literal("ligand-parent-delivery-v1"),
  workflow_id: z.string().regex(/^ligand-flow-[a-f0-9]{24}$/),
  evidence_id: z.string().regex(/^hyp-[a-f0-9]{24}$/),
  proposal_id: z.string().regex(/^proposal-[a-f0-9]{12}$/),
  proposal_sha256: digest,
  hash_scope: z.string(), compute_submitted: z.literal(false),
  files: z.array(z.object({ name: z.enum(["hypothesis.json", "report.md"]),
    mime_type: z.enum(["application/json", "text/markdown"]), sha256: digest,
    bytes: z.number().int().min(0).max(2 * 1024 * 1024),
    base64: z.string().max(2796204),
  }).strict()).length(2),
}).strict()

const design = parent.omit({ schema: true, proposal_id: true, proposal_sha256: true, files: true }).extend({
  schema: z.literal("ligand-design-delivery-v1"),
  design_id: z.string().regex(/^design-[a-f0-9]{24}$/),
  delivery_manifest_sha256: digest,
  files: z.array(parent.shape.files.element.omit({ name: true }).extend({
    name: z.enum(["disposition.json", "report.md"]),
  }).strict()).length(2),
}).strict()
const covdock = design.omit({ schema: true, files: true }).extend({
  schema: z.literal("ligand-covdock-delivery-v1"),
  report_id: z.string().regex(/^ligand-covdock-report-[a-f0-9]{24}$/),
  files: z.array(parent.shape.files.element.omit({ name: true }).extend({
    name: z.enum(["report.json", "report.md"]),
  }).strict()).length(2),
}).strict()
const schema = z.discriminatedUnion("schema", [parent, design, covdock])

/** Called only for a host-bound native export, never for model-supplied bytes. */
export async function publishParentDelivery(result: unknown, scope: { project: string; session: string }, workflow: string) {
  const envelope = z.object({ isError: z.boolean().optional(), structuredContent: z.unknown().optional(),
    content: z.array(z.object({ type: z.string(), text: z.string().optional() }).passthrough()).optional(),
  }).passthrough().parse(result)
  if (envelope.isError) return result
  const value = z.record(z.string(), z.unknown()).parse(envelope.structuredContent ??
    JSON.parse(envelope.content?.find(item => item.type === "text")?.text ?? "null"))
  const delivery = schema.parse(value.result)
  if (delivery.workflow_id !== workflow || new Set(delivery.files.map(file => file.name)).size !== 2)
    throw Error("Parent delivery identity or file set mismatch")
  const files = delivery.files.map(file => {
    const bytes = Buffer.from(file.base64, "base64")
    if (bytes.toString("base64") !== file.base64 || bytes.length !== file.bytes ||
        new Bun.CryptoHasher("sha256").update(bytes).digest("hex") !== file.sha256 ||
        file.mime_type !== (file.name === "report.md" ? "text/markdown" : "application/json"))
      throw Error("Parent artifact integrity mismatch")
    if (file.name === "hypothesis.json" && delivery.schema === "ligand-parent-delivery-v1") {
      const proposal = z.object({ evidence_id: z.string(), hypothesis: z.object({ route: z.literal("ligand-first") }) }).parse(JSON.parse(bytes.toString("utf8")))
      if (file.sha256 !== delivery.proposal_sha256 || proposal.evidence_id !== delivery.evidence_id)
        throw Error("Parent proposal provenance mismatch")
    }
    if (file.name === "disposition.json" && delivery.schema === "ligand-design-delivery-v1") {
      const disposition = z.object({ design_id: z.string(), evidence_id: z.string() }).parse(JSON.parse(bytes.toString("utf8")))
      if (disposition.design_id !== delivery.design_id || disposition.evidence_id !== delivery.evidence_id)
        throw Error("Design disposition provenance mismatch")
    }
    if (file.name === "report.json" && delivery.schema === "ligand-covdock-delivery-v1") {
      const report = z.object({ schema: z.literal("ligand-covdock-report-v1"), workflow_id: z.string(),
        design_id: z.string(), evidence_id: z.string(), compute_submitted_by_report: z.literal(false) }).parse(JSON.parse(bytes.toString("utf8")))
      if (report.workflow_id !== delivery.workflow_id || report.design_id !== delivery.design_id ||
          report.evidence_id !== delivery.evidence_id)
        throw Error("CovDock report provenance mismatch")
    }
    return { ...file, bytes }
  })
  const identity = delivery.schema === "ligand-parent-delivery-v1" ? delivery.proposal_id :
    delivery.schema === "ligand-covdock-delivery-v1" ? delivery.report_id : delivery.design_id
  using lock = await Lock.write(`parent-delivery:${scope.project}:${workflow}`)
  const existing = await ArtifactStore.list(scope.project)
  const artifacts = []
  for (const file of files) {
    const source = `native/${workflow}/${delivery.evidence_id}/${identity}/${file.name}`
    const prior = existing.find(item => item.current.sourcePath === source && item.current.sha256 === file.sha256)
    const valid = prior && await ArtifactStore.read(scope.project, prior.id, prior.currentVersionID)
    const artifact = valid ? prior : await ArtifactStore.save({ projectID: scope.project, sessionID: scope.session,
      sourcePath: source, filename: file.name, kind: file.name === "report.md" ? "report" : "file",
      content: new Blob([file.bytes]), mimeType: file.mime_type, captureQuality: "exact",
      title: `Ligand ${identity} ${file.name}` })
    artifacts.push({ artifact_id: artifact.id, version_id: artifact.currentVersionID,
      name: file.name, sha256: file.sha256, bytes: file.bytes.length })
  }
  const summary = { ...value, result: { ...delivery, files: undefined, artifacts,
    state: delivery.schema === "ligand-parent-delivery-v1" ? "PARENT_REPORT_DELIVERED" :
      delivery.schema === "ligand-covdock-delivery-v1" ? "COVDOCK_REPORT_DELIVERED" : "DESIGN_REPORT_DELIVERED", next_action: "Official files are in Results; no remote path access or rewriting is needed." } }
  return { ...envelope, structuredContent: summary, content: [{ type: "text", text: JSON.stringify(summary) }] }
}
