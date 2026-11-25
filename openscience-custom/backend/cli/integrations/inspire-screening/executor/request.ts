import { createHash } from "node:crypto"
import type { PluginInput } from "@synsci/plugin"
import type { ToolContext } from "@synsci/plugin/tool"
import z from "zod"

export const origin = z.object({
  user_message_id: z.string().min(1), assistant_message_id: z.string().min(1),
  part_ids: z.array(z.string().min(1)).min(1), request_sha256: z.string().regex(/^[a-f0-9]{64}$/),
}).strict()
export type BoundRequest = { text: string; origin: z.infer<typeof origin> }
export const requestHash = (text: string) => createHash("sha256").update(text).digest("hex")

type Message = { info: { id: string; role: string; sessionID: string; parentID?: string };
  parts: { id: string; messageID: string; sessionID: string; type: string; text?: string; synthetic?: boolean; ignored?: boolean }[] }

/** Shared by the native plugin and the in-process workflow session bridge. */
export function requestFromMessages(assistant: Message | undefined, user: Message | undefined,
  context: Pick<ToolContext, "sessionID" | "messageID">): BoundRequest {
  if (!assistant || assistant.info.role !== "assistant" || assistant.info.id !== context.messageID || assistant.info.sessionID !== context.sessionID)
    throw new Error("Cannot bind screening request to the actual tool-calling assistant")
  if (!user || user.info.role !== "user" || user.info.id !== assistant.info.parentID || user.info.sessionID !== context.sessionID)
    throw new Error("Cannot bind screening request to its original user message")
  const parts = user.parts.filter(part => part.type === "text" && !part.synthetic && !part.ignored)
  const text = parts.map(part => part.text ?? "").join("\n")
  if (!text.trim() || text.length > 10000) throw new Error("Original request must contain 1–10000 text characters; clarify rather than truncate or infer from attachments")
  if (parts.some(part => part.messageID !== user.info.id || part.sessionID !== context.sessionID)) throw new Error("Request part belongs to another conversation/message")
  return { text, origin: origin.parse({ user_message_id: user.info.id, assistant_message_id: assistant.info.id,
    part_ids: parts.map(part => part.id), request_sha256: requestHash(text) }) }
}

// Use the actual tool-calling assistant's parent, not the latest message in a
// concurrently edited conversation or the model's restatement of user intent.
export async function bindRequest(client: PluginInput["client"], context: Pick<ToolContext, "sessionID" | "messageID" | "directory">): Promise<BoundRequest> {
  const assistant = (await client.session.message({ path: { id: context.sessionID, messageID: context.messageID },
    query: { directory: context.directory }, throwOnError: true })).data
  if (!assistant || assistant.info.role !== "assistant" || assistant.info.id !== context.messageID || assistant.info.sessionID !== context.sessionID) {
    throw new Error("Cannot bind screening request to the actual tool-calling assistant")
  }
  const user = (await client.session.message({ path: { id: context.sessionID, messageID: assistant.info.parentID },
    query: { directory: context.directory }, throwOnError: true })).data
  if (!user || user.info.role !== "user" || user.info.id !== assistant.info.parentID || user.info.sessionID !== context.sessionID) {
    throw new Error("Cannot bind screening request to its original user message")
  }
  return requestFromMessages(assistant, user, context)
}
