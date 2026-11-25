import { open, link, unlink } from "node:fs/promises"
import { randomUUID } from "node:crypto"

/** Publish a complete, immutable receipt without replacing a concurrent winner.
 * Compilation has no external side effects, so a crashed unpublished temporary
 * file need not block retry. Readers only inspect the final name.
 */
export async function publishFrozenJSON(filename: string, value: unknown) {
  const temporary = `${filename}.${randomUUID()}.pending`
  const file = await open(temporary, "wx", 0o600)
  try {
    await file.writeFile(JSON.stringify(value, null, 2))
    await file.sync()
  } finally {
    await file.close()
  }
  try {
    await link(temporary, filename)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error
  } finally {
    await unlink(temporary)
  }
}
