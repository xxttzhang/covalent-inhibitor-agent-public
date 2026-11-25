import z from "zod"
import { digest, type Task } from "./model"
import { mdCandidateChoice } from "./workflow-refinement-results"
import type { Publications } from "./publication"
import type { Store } from "./store"
import { readRefinementObservation } from "./refinement-lifecycle"
import { refinementApprovalReference } from "./refinement-execution"
import { RefinementWorkflows } from "./refinement-workflow"
import { Refinements } from "./refinement-approval"

const keys = ["candidate_table", "covdock_structure"] as const
export const mdNativeTarget = z.object({ schema: z.literal("library-md-native-target-v1"), choice_id: mdCandidateChoice.shape.id,
  native_workflow_id: mdCandidateChoice.shape.native_workflow_id, native_workflow_digest: mdCandidateChoice.shape.native_workflow_digest,
  task_id: mdCandidateChoice.shape.task_id, artifact_id: z.string().min(1).max(200), version_id: z.string().min(1).max(200),
  artifact_sha256: z.string().regex(/^[a-f0-9]{64}$/), parameters: z.object({ chain: z.string().min(1).max(10),
    residue: z.literal("CYS"), residue_number: z.number().int().positive(), reaction_type: z.string().min(1).max(200) }).strict(),
}).strict()
export const mdTransfer = z.object({ schema: z.literal("library-md-transfer-v1"),
  choice_id: mdCandidateChoice.shape.id, files: mdCandidateChoice.shape.files,
  offsets: z.object({ candidate_table: z.number().int().nonnegative(), covdock_structure: z.number().int().nonnegative() }).strict(),
  state: z.enum(["receiving", "frozen"]), native_target_context: mdNativeTarget.optional(),
}).strict()
export const mdInput = z.object({ choice_id: mdCandidateChoice.shape.id,
  artifact_id: z.string().regex(/^candidate-pose-input-[a-f0-9]{32}$/),
  manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/), status: z.literal("pose_selected_native_graph_qc_pending"),
  native_graph_qc_completed: z.literal(false), parameterization_completed: z.literal(false),
  md_system_ready: z.literal(false), compute_submitted: z.literal(false),
}).strict()
export const mdChunk = z.object({ choice_id: mdCandidateChoice.shape.id, file: z.enum(keys),
  offset: z.number().int().nonnegative(), data: z.string().min(1).max(174764) }).strict()
const size = 128 * 1024

/** Recover target/reaction from the same approved completed native plan. The
 * candidate display table is never authority for a receptor or reaction. */
export function readMdNativeTarget(store: Store, parent: Task, reference: z.infer<typeof refinementApprovalReference>,
  choice: z.infer<typeof mdCandidateChoice>) {
  const observed = readRefinementObservation(store, parent, reference)
  if (!observed.approval_recorded || observed.execution_state !== "completed"
    || choice.task_id !== parent.id || choice.native_workflow_id !== reference.workflow_id || choice.native_workflow_digest !== reference.digest)
    throw Error("MD native context requires the exact approved completed refinement")
  const workflow = new RefinementWorkflows(new Refinements(store)).get(reference.workflow_id)
  const stage = workflow.plan.stages[1]
  if (stage.engine.name !== "covdock" || stage.runtime.name !== "covdock") throw Error("MD source has no native CovDock target")
  const target = /^([^:]+):(\d+)$/.exec(stage.runtime.attachment_residue)
  if (!target) throw Error("Unsupported native CovDock attachment residue")
  const file = choice.files.covdock_structure
  return mdNativeTarget.parse({ schema: "library-md-native-target-v1", choice_id: choice.id,
    native_workflow_id: workflow.id, native_workflow_digest: workflow.digest, task_id: parent.id,
    artifact_id: file.artifact_id, version_id: file.version_id, artifact_sha256: file.sha256,
    parameters: { chain: target[1], residue: "CYS", residue_number: Number(target[2]), reaction_type: stage.engine.reaction } })
}

/** Read the exact published version, including the already acknowledged prefix.
 * Only unacknowledged bytes cross the private MCP channel. A failed exchange
 * stops this invocation; reopening the backend recovers its durable offsets. */
export async function transferMdFiles(choice: z.infer<typeof mdCandidateChoice>, transfer: z.infer<typeof mdTransfer>,
  publications: Pick<Publications, "request">, parent: Task, signal: AbortSignal,
  send: (chunk: z.infer<typeof mdChunk>) => Promise<void>) {
  if (transfer.choice_id !== choice.id || digest(transfer.files) !== digest(choice.files)
    || parent.id !== choice.task_id) throw Error("MD transfer differs from the selected native files")
  for (const key of keys) {
    signal.throwIfAborted()
    const file = choice.files[key]
    const offset = transfer.offsets[key]
    if (offset > file.bytes || transfer.state === "frozen" && offset !== file.bytes)
      throw Error("Backend MD byte offset exceeds the selected file")
    const response = await publications.request(parent,
      `/file/artifact-store/${encodeURIComponent(file.artifact_id)}/raw?versionID=${encodeURIComponent(file.version_id)}`)
    const reader = response.body?.getReader()
    if (!reader) throw Error("Selected MD publication has no readable body")
    const hash = new Bun.CryptoHasher("sha256")
    const progress = { bytes: 0 }
    try {
      while (true) {
        signal.throwIfAborted()
        const chunk = await reader.read()
        signal.throwIfAborted()
        if (chunk.done) break
        const start = progress.bytes
        progress.bytes += chunk.value.byteLength
        if (progress.bytes > file.bytes) throw Error("Selected MD publication exceeds its bound size")
        hash.update(chunk.value)
        for (const segment of segments(chunk.value, start, offset)) {
          signal.throwIfAborted()
          await send({ choice_id: choice.id, file: key, offset: segment.offset,
            data: Buffer.from(segment.bytes).toString("base64") })
        }
      }
      if (progress.bytes !== file.bytes || hash.digest("hex") !== file.sha256)
        throw Error("Selected MD publication bytes differ from the bound version")
    } finally {
      await reader.cancel().catch(() => {})
      reader.releaseLock()
    }
  }
}

function* segments(bytes: Uint8Array, start: number, acknowledged: number) {
  const first = Math.max(0, acknowledged - start)
  for (const index of Array.from({ length: Math.max(0, Math.ceil((bytes.byteLength - first) / size)) }, (_, i) => first + i * size))
    yield { offset: start + index, bytes: bytes.subarray(index, Math.min(index + size, bytes.byteLength)) }
}
