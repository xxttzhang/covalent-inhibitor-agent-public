import type { Agent } from "../agent/agent"
import { PermissionNext } from "../permission/next"
import { Session } from "."

/** Each session is a separate ceiling; an ancestor allow cannot erase a child deny. */
export async function sessionPermissionScopes(sessionID: string): Promise<PermissionNext.Ruleset[]> {
  const scopes: PermissionNext.Ruleset[] = []
  const seen = new Set<string>()
  const current = await Session.get(sessionID)
  const visit = async (session: Session.Info): Promise<void> => {
    if (seen.has(session.id)) throw Error("Cyclic session permission ancestry")
    if (session.projectID !== current.projectID || session.directory !== current.directory)
      throw Error("Session permission ancestry crosses project or directory")
    seen.add(session.id)
    scopes.push(session.permission ?? [])
    if (session.parentID) await visit(await Session.get(session.parentID))
  }
  await visit(current)
  return scopes
}

/** Refresh ceilings both before approval and after an approval wait. */
export async function askToolPermission(input: Parameters<typeof PermissionNext.ask>[0]) {
  await PermissionNext.ask({ ...input,
    constraints: [...input.constraints ?? [], ...await sessionPermissionScopes(input.sessionID)] })
  for (const scope of await sessionPermissionScopes(input.sessionID)) {
    for (const pattern of input.patterns) {
      const rules = input.permission === "mcp" ? PermissionNext.mcpRules(pattern, scope) : scope
      const rule = PermissionNext.evaluate(input.permission, pattern,
        [{ permission: "*", pattern: "*", action: "allow" }], rules)
      if (rule.action === "deny") throw new PermissionNext.DeniedError([rule])
    }
  }
}

/** Refresh at dispatch: a tool advertised earlier does not retain permission. */
export async function toolPermissions(agent: Pick<Agent.Info, "permission">, sessionID: string, mcp?: string) {
  const rules = PermissionNext.merge(agent.permission, (await Session.get(sessionID)).permission ?? [])
  return mcp ? PermissionNext.mcpRules(mcp, rules) : rules
}

/** Whole-tool denial complements each tool's argument-specific permission check. */
export async function checkToolPermission(name: string, agent: Pick<Agent.Info, "permission">, sessionID: string, mcp = false) {
  const rules = await toolPermissions(agent, sessionID, mcp ? name : undefined)
  for (const scope of [rules, ...await sessionPermissionScopes(sessionID)]) {
    if (PermissionNext.toolDenied(name, scope, mcp)) throw new PermissionNext.DeniedError(scope)
  }
}

/** Legacy per-tool toggles update only their own scope, preserving other denials. */
export async function updateToolPermissions(sessionID: string, tools: Record<string, boolean>) {
  const permissions = Object.entries(tools).map(([permission, enabled]) => ({
    permission, pattern: "*", action: enabled ? "allow" as const : "deny" as const,
  }))
  return Session.update(sessionID, draft => {
    draft.permission = PermissionNext.merge(draft.permission ?? [], permissions)
  })
}
