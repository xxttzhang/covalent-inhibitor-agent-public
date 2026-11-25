import z from "zod"
import path from "node:path"
import { mkdir, writeFile } from "node:fs/promises"
import { Tool } from "./tool"
import { Instance } from "../project/instance"
import { MessageV2 } from "../session/message-v2"
import { directRequest, folder, hash, invoke, registered } from "../../integrations/md-workflow/registered"

export const MolecularDynamicsTool = Tool.define("molecular_dynamics", {
  description: "Plan, start, or inspect the host-registered Library-first eight-candidate MD+analysis workflow. Fixed OpenMM/OPLS_2005 covalent-adduct profile, 3 x 10 ns each, maximum 8 GPUs and 60 GPU-hours. No arbitrary commands, structures or extra replicas. Start requires the actual user's direct request and native exact-plan resource permission. Never present job success as scientific completion. If inputs/profile are not ready, this tool refuses submission. Status/plan are read-only. No prereactive NAC conclusions from adduct trajectories.",
  parameters: z.object({ action: z.enum(["plan", "start", "status"]) }).strict(),
  async execute(args, ctx) {
    const { settings, digest } = await registered(Instance.directory)
    if (args.action !== "start") {
      const state = await invoke(args.action)
      return { title: "Library-first MD · " + args.action, metadata: { profile_digest: digest, ...state }, output: JSON.stringify(state, null, 2) }
    }
    const assistant = await MessageV2.get({ sessionID: ctx.sessionID, messageID: ctx.messageID })
    if (assistant.info.role !== "assistant") throw Error("Missing original request binding")
    const user = await MessageV2.get({ sessionID: ctx.sessionID, messageID: assistant.info.parentID })
    if (user.info.role !== "user") throw Error("Missing user request")
    const text = user.parts.filter(p => p.type === "text" && !p.synthetic && !p.ignored).map(p => p.type === "text" ? p.text : "").join("\n")
    if (!directRequest(text)) throw Error("需要明确执行指令，例如：开始 Library-first 的 MD 和自动分析。提问或附件不会启动计算。")
    const plan = await invoke("plan")
    if (plan.submission_already_reserved === true) {
      const state = await invoke("status")
      return { title: "Library-first MD · 已登记，未重复提交", metadata: state, output: JSON.stringify(state, null, 2) }
    }
    await ctx.ask({ permission: "remote_compute", patterns: [digest], always: [], metadata: {
      route: "library-md", profile_digest: digest, execution_plan_sha256: settings.plan_sha256,
      purpose: "Library-first：短程 QC → 三次独立 10 ns MD → 全轨迹审计 → 自动分析与图表",
      candidates: 8, max_gpus: 8, max_gpu_hours: 60, engine: settings.engine, forcefield: settings.forcefield,
      chemical_state: settings.chemical_state, immutable_profile: settings, live_plan: plan,
      warning: "不自动扩量、续跑或重试。QC 失败即停止该候选；保留失败结果。MD 稳定性不代表亲和力或 kinact；共价加合物不输出反应前 NAC。",
    } })
    ctx.abort.throwIfAborted()
    if ((await registered(Instance.directory)).digest !== digest) throw Error("MD profile changed after approval")
    const authorization = path.join(folder, "authorizations", hash(ctx.sessionID + user.info.id + digest) + ".json")
    await mkdir(path.dirname(authorization), { recursive: true, mode: 0o700 })
    await writeFile(authorization, JSON.stringify({ schema: "md-native-authorization-v1", mechanism: "native_remote_compute",
      profile_sha256: digest, plan_sha256: settings.plan_sha256, session_id: ctx.sessionID,
      project_id: Instance.project.id, directory: Instance.directory, user_message_id: user.info.id,
      user_text_sha256: hash(text), approved_at: Date.now(), max_gpu_hours: 60 }, null, 2), { flag: "wx", mode: 0o600 })
    const state = await invoke("start", authorization)
    return { title: "Library-first MD · 提交控制器", metadata: state, output: JSON.stringify(state, null, 2) }
  },
})
