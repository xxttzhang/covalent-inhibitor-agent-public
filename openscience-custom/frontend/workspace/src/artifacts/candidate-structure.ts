// Display links are explicit immutable artifact references, never guessed paths.
export function candidateStructures(row: Record<string, string>) {
  return (["af3", "covdock", "generated"] as const).flatMap((method) => {
    if (method === 'generated' && row.route !== 'de-novo') return []
    const id = row[`${method}_artifact_id`]
    const version = row[`${method}_artifact_version`]
    const sha256 = row[`${method}_artifact_sha256`]
    if (!id || !version || !/^[a-f0-9]{64}$/.test(sha256 ?? "")) return []
    const pose = row.covdock_structure_pose
    if (method === "covdock" && !/^\d+$/.test(pose ?? "")) return []
    return [{ method, id, version, sha256, pose: method === "covdock" ? Number(pose) : undefined }]
  })
}

export function verifyStructure(bytes: Uint8Array, expected: string) {
  return crypto.subtle.digest("SHA-256", bytes as Uint8Array<ArrayBuffer>).then((hash) => {
    const actual = Array.from(new Uint8Array(hash), (value) => value.toString(16).padStart(2, "0")).join("")
    if (actual !== expected) throw new Error("结构文件校验失败；拒绝显示可能错配的构象。")
  })
}
