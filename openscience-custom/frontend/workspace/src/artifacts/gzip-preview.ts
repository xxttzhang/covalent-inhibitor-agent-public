// Browser gzip streams reject concatenated members. Candidate boundaries are
// accepted only after the native decoder validates a complete member (CRC/size).
// Header-like bytes inside compressed data are never trusted as boundaries.
export async function gzipPreview(bytes: Uint8Array<ArrayBuffer>, limit = 192 * 1024 * 1024) {
  const starts: number[] = []
  for (let i = 0; i + 10 <= bytes.length; i++) {
    if (bytes[i] === 31 && bytes[i + 1] === 139 && bytes[i + 2] === 8 && !(bytes[i + 3] & 224)) starts.push(i)
    if (starts.length > 4096) throw new Error("Too many gzip boundary candidates")
  }
  if (starts[0] !== 0) throw new Error("Invalid gzip header")
  starts.push(bytes.length)
  const parts: Uint8Array[] = []
  let total = 0
  let work = 0
  for (let member = 0; member < starts.length - 1;) {
    let accepted = false
    for (let next = member + 1; next < starts.length; next++) {
      work += starts[next] - starts[member]
      if (work > 512 * 1024 * 1024) throw new Error("Gzip preview work limit exceeded")
      const stream = new Blob([bytes.subarray(starts[member], starts[next])]).stream().pipeThrough(new DecompressionStream("gzip"))
      const reader = stream.getReader()
      const chunks: Uint8Array[] = []
      let size = 0
      let exceeded = false
      try {
        while (true) {
          const chunk = await reader.read()
          if (chunk.done) break
          size += chunk.value.length
          if (total + size > limit) { exceeded = true; throw new Error("Gzip preview decompressed limit exceeded") }
          chunks.push(chunk.value)
        }
        parts.push(...chunks)
        total += size
        member = next
        accepted = true
      } catch (error) {
        if (exceeded) throw error
      } finally {
        await reader.cancel().catch(() => undefined)
      }
      if (accepted) break
    }
    if (!accepted) throw new Error("Invalid or truncated gzip member")
  }
  const output = new Uint8Array(total)
  let offset = 0
  for (const part of parts) { output.set(part, offset); offset += part.length }
  return output
}
