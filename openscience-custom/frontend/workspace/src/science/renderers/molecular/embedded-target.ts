import z from "zod"
import { targetContext, type TargetContext } from "@synsci/util/screening-target"

export const targetMarker = "REMARK 900 OPENSCIENCE_TARGET_V1 "
const binding = z.object({
  task_id: z.string().regex(/^denovo-[a-f0-9]{24}$/),
  plan_digest: z.string().regex(/^[a-f0-9]{64}$/),
  structure_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  source_receptor_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  target: z.string().regex(/^[A-Za-z0-9_.-]{1,64}$/),
  chain: z.string().regex(/^[A-Za-z0-9]$/), residue: z.string().regex(/^[A-Z0-9]{3}$/),
  residue_number: z.number().int().min(-999).max(9999), insertion_code: z.string().regex(/^[A-Za-z0-9]?$/),
  atom: z.string().regex(/^[A-Za-z0-9]{1,4}$/),
  reaction_type: z.string().regex(/^[A-Za-z0-9 +()_-]{1,80}$/),
  ligand: z.object({ chain: z.string().regex(/^[A-Za-z0-9]$/), residue: z.string().regex(/^[A-Z0-9]{3}$/),
    residue_number: z.number().int(), atom: z.string().regex(/^[A-Za-z0-9]{1,4}$/) }).strict(),
}).strict()

async function hash(text: string) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))
  return Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, "0")).join("")
}

// A versioned display declaration, never a geometric guess or an execution
// instruction. Bind it to exact file bytes and verify the declared atom exists.
export async function embeddedTarget(source: string): Promise<{ context: TargetContext; atom: string; ligand: z.infer<typeof binding>["ligand"] } | undefined> {
  const lines = source.split("\n")
  const marked = lines.filter((line) => line.startsWith(targetMarker))
  if (!marked.length) return
  if (marked.length !== 1) throw new Error("结构文件含多个位点声明，无法自动定位。")
  const record = binding.parse(JSON.parse(marked[0].slice(targetMarker.length)))
  const body = lines.filter((line) => !line.startsWith(targetMarker)).join("\n")
  if (await hash(body) !== record.structure_sha256) throw new Error("结构与位点记录的校验不一致，停止自动高亮。")
  const atoms = lines.filter((line) => line.startsWith("ATOM  ") && line[21] === record.chain &&
    line.slice(17, 20) === record.residue && Number(line.slice(22, 26)) === record.residue_number &&
    line.slice(26, 27).trim() === record.insertion_code && line.slice(12, 16).trim() === record.atom)
  if (atoms.length !== 1 || ![30, 38, 46].every((i) => Number.isFinite(Number(atoms[0].slice(i, i + 8)))))
    throw new Error("声明的共价位点原子缺失或不唯一，停止自动高亮。")
  const digest = await hash(source)
  const ligand = lines.filter((line) => line.startsWith("HETATM") && line[21] === record.ligand.chain &&
    line.slice(17, 20) === record.ligand.residue && Number(line.slice(22, 26)) === record.ligand.residue_number &&
    line.slice(12, 16).trim() === record.ligand.atom)
  if (ligand.length !== 1) throw new Error("反应碳原子缺失或不唯一，停止自动显示连线。")
  const context = targetContext.parse({ task_id: record.task_id, plan_digest: record.plan_digest, artifact_sha256: digest,
    target: record.target, site: `${record.chain}:${record.residue}:${record.residue_number}${record.insertion_code}:${record.atom}`,
    parameters: { target: record.target, receptor: "embedded-structure", receptor_sha256: digest,
      chain: record.chain, residue: record.residue, residue_number: record.residue_number, insertion_code: record.insertion_code,
      numbering: "structure", reaction_type: record.reaction_type } })
  return { context, atom: record.atom, ligand: record.ligand }
}
