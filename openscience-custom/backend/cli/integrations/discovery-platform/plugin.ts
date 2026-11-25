import type { Plugin } from "@synsci/plugin"
import { Project } from "../../src/project/project"
import { ProjectTrust } from "../../src/project/trust"
import { Config } from "../../src/config/config"
import { start } from "./runtime"
import { studyTool } from "./study-tool"
import { Instance } from "../../src/project/instance"
import { installStudyHTTP } from "./study-http"
import { guardStudyReasoning } from "./study-reasoning-guard"
import path from "node:path"
import { chmod } from "node:fs/promises"
import { root, directory as nativeDirectory, open as openNative } from "../inspire-screening/executor/host"
import { ResearchLoopStore } from "./research-loop-store"
import { createResearchHost, loadResearchHostCatalog } from "./research-host"
import { researchTool } from "./research-tool"
import { evaluateResearchNativeAction, recoverResearchNativeEvaluation } from "./research-native-evaluation"
import { publish } from "./runtime"
const DiscoveryPlugin: Plugin = async input => {
  const { Server } = await import("../../src/server/server")
  installStudyHTTP(Server.App(), () => ({ directory: Instance.directory, project: Instance.project.id,
    authorize: async () => {
      await ProjectTrust.require((await Project.resolve(Instance.project.id, Instance.directory)).project, "local_job")
    },
    localChemistry: async () => !(await Config.trustedSandbox()).enabled,
    authorizeRemote: async () => {
      await ProjectTrust.require((await Project.resolve(Instance.project.id, Instance.directory)).project, "remote_job")
    },
  }))
  const authorize = async () => {
    await ProjectTrust.require((await Project.resolve(input.project.id, input.directory)).project, "local_job")
    if ((await Config.trustedSandbox()).enabled) throw Error("本地分析需要通过项目已授权的执行环境运行")
  }
  const local = async () => { await ProjectTrust.require((await Project.resolve(input.project.id, input.directory)).project, "local_job") }
  const remote = async () => { await ProjectTrust.require((await Project.resolve(input.project.id, input.directory)).project, "remote_job") }
  // This installation setting is captured once. Neither a tool argument nor
  // project-authored JSON can label its own provider prices as verified.
  const expected = process.env.OPENSCIENCE_RESEARCH_HOST_CATALOG_SHA256
  const catalog = await loadResearchHostCatalog(path.join(root, "configs/research-host.json"), expected)
  const controller = new AbortController()
  const state: { host?: Promise<ReturnType<typeof createResearchHost>>; close?: () => void; ready: boolean } = { ready: false }
  const host = () => state.host ??= (async () => {
    const native = await openNative()
    const research = new ResearchLoopStore(path.join(nativeDirectory, "research-host.sqlite"))
    await chmod(path.join(nativeDirectory, "research-host.sqlite"), 0o600)
    state.close = () => { research.close(); native.close() }
    return createResearchHost({ directory: input.directory, project: input.project.id, research, native,
      catalog, catalog_digest: expected ?? null, authorizeRemote: remote, authorizeEvaluation: authorize,
      evaluation: { evaluate: evaluateResearchNativeAction, recover: recoverResearchNativeEvaluation } })
  })()
  const observer = catalog?.specs.some(spec => spec.workspace_project === input.project.id && spec.contract.directory === input.directory)
    ? async () => {
      // Avoid model/plugin recursion while the platform installs this plugin.
      if (!state.ready || controller.signal.aborted) return
      await Instance.provide({ directory: input.directory, fn: async () => {
        await local()
        const loops = await (await host()).resume(controller.signal)
        if (loops.length) await publish(input.directory, ".discovery/research-host-status.json", {
          schema: "drug-design-research-host-status-v1", observed_at: new Date().toISOString(), loops,
        })
      } })
    } : undefined
  const service = await start(input.directory, input.project.id, import.meta.url, authorize, observer)
  state.ready = true
  return { tool: { discovery_study: studyTool(input.directory, input.project.id, async () => {
    await ProjectTrust.require((await Project.resolve(input.project.id, input.directory)).project, "local_job")
  }, async () => !(await Config.trustedSandbox()).enabled, async () => {
    await ProjectTrust.require((await Project.resolve(input.project.id, input.directory)).project, "remote_job")
  }), discovery_research: researchTool(host, local) },
    "tool.execute.before": async (request, output) => guardStudyReasoning(input.directory, input.project.id, request, output.args),
    event: async ({ event }) => { if (event.type === "server.instance.disposed") {
      controller.abort(); await service.stop(); state.close?.()
    } } }
}
export default DiscoveryPlugin
