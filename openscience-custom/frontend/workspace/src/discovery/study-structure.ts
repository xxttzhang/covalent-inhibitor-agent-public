import type { StudyCandidateSource } from "@synsci/util/discovery-study"

/** Adapt only explicit immutable references to the existing audited viewer. */
export function studyStructure(source: StudyCandidateSource, index: number): { method: "af3" | "covdock" | "generated"; row: Record<string, string> } {
  const ref = source.candidate.structures[index]
  if (!ref || ref.asset.storage !== "artifact") throw Error("此结构尚无可用于已有查看器的不可变产物引用")
  if (ref.identity.compound_id !== source.candidate.id) throw Error("结构候选身份与来源记录不一致")
  if (ref.method !== "af3" && ref.method !== "covdock" && ref.method !== "generated") throw Error("此方法尚未适配结构查看器")
  if (ref.method === "covdock" && ref.pose === undefined) throw Error("缺少明确姿势索引，不能默认展示第一个构象")
  return { method: ref.method, row: { ...ref.identity, route: source.route,
    [`${ref.method}_artifact_id`]: ref.asset.id,
    [`${ref.method}_artifact_version`]: ref.asset.version,
    [`${ref.method}_artifact_sha256`]: ref.asset.sha256,
    ...(ref.pose === undefined ? {} : { covdock_structure_pose: String(ref.pose) }),
  } as Record<string, string> }
}
