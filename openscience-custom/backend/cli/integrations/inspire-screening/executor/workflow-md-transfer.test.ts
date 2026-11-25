import { expect, test } from "bun:test"
import path from "node:path"
import z from "zod"
import { tmpdir } from "../../../test/fixture/fixture"
import { Store } from "./store"
import { Publications } from "./publication"
import { fixture } from "./workflow-plan.fixture"
import { prepareWorkflowScreening } from "./workflow-plan"
import { mdCandidateChoice } from "./workflow-refinement-results"
import { mdTransfer, mdInput, transferMdFiles } from "./workflow-md-transfer"

const workspace = path.resolve(import.meta.dir, "../../../../../..")
const schema = z.object({ md_candidate: mdCandidateChoice, md_transfer: mdTransfer.nullable(), md_input: mdInput.nullable() }).passthrough()
const bootstrap = `import json,os,sys
from pathlib import Path
from pytest import MonkeyPatch
from covscreen.native_approval import decode
from covscreen.workflow_store import load
from test_workflow_md_transfer import research,ready,send
root=Path(sys.argv[1]); command=json.load(sys.stdin)
if command["operation"]=="init":
    with MonkeyPatch.context() as patch:
        context=research.__wrapped__(root,patch)
        files,value,_=ready(context)
        metadata={"folder":str(context[1]),"source":context[2],"scope":context[3],"report":value}
        (root/"context.json").write_text(json.dumps(metadata))
        for name,data in files.items(): (root/name).write_bytes(data)
        choice=load(context[1])["md_candidate"]
        result=send(context,"md_transfer_open",md_choice_id=choice["id"])
else:
    os.environ["COVALENT_APPROVAL_KEY_FILE"]=str(root/"test-only.key")
    metadata=json.loads((root/"context.json").read_text())
    context=(root,Path(metadata["folder"]),metadata["source"],metadata["scope"])
    payload=command.get("payload",{})
    if command["operation"]=="md_transfer_commit": payload["refinement_report"]=metadata["report"]
    result=send(context,command["operation"],**payload)
os.environ["COVALENT_APPROVAL_KEY_FILE"]=str(root/"test-only.key")
print(json.dumps(decode(result["_host_handoff"])))
`

async function backend(root: string, operation: string, payload?: Record<string, unknown>) {
  const child = Bun.spawn([path.join(workspace, ".cache/refinement-validation-python310/bin/python"), "-B", "-c", bootstrap, root], {
    stdin: "pipe", stdout: "pipe", stderr: "pipe", env: { ...process.env,
      PYTHONPATH: ["science/src", "science/tests"].map(name => path.join(workspace, name)).join(path.delimiter) },
  })
  child.stdin.write(JSON.stringify({ operation, payload }))
  child.stdin.end()
  const [stdout, stderr, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
  if (code !== 0) throw Error(stderr)
  return schema.parse(JSON.parse(stdout))
}

test("actual HTTP bytes cross signed backend chunks, resume after lost acknowledgement and freeze with the existing producer", async () => {
  await using tmp = await tmpdir({ git: true })
  const first = await backend(tmp.path, "init")
  const choice = first.md_candidate
  const store = new Store(":memory:")
  const f = fixture(tmp.path)
  const native = prepareWorkflowScreening(store, f.intent, f.scope, f.environment).task
  // The HTTP reader uses only task identity/directory. Native report/approval
  // admission is separately exercised by workflow-results/refinement tests.
  const parent = { ...native, id: choice.task_id }
  const requests: string[] = []
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, fetch(request) {
    const url = new URL(request.url)
    requests.push(url.pathname + url.search)
    const key = (Object.keys(choice.files) as Array<keyof typeof choice.files>).find(key => {
      const file = choice.files[key]
      return url.pathname === `/file/artifact-store/${file.artifact_id}/raw` && url.searchParams.get("versionID") === file.version_id
    })
    return key ? new Response(Bun.file(path.join(tmp.path, key))) : new Response("missing", { status: 404 })
  } })
  try {
    const publications = new Publications(store, server.url.toString())
    const stop = new AbortController()
    const calls = { count: 0 }
    await expect(transferMdFiles(choice, first.md_transfer!, publications, parent, stop.signal, async chunk => {
      await backend(tmp.path, "md_transfer_chunk", { md_chunk: chunk })
      calls.count++
      throw Error("software lost acknowledgement after durable backend commit")
    })).rejects.toThrow("lost acknowledgement")
    const resumed = await backend(tmp.path, "md_transfer_open", { md_choice_id: choice.id })
    expect(resumed.md_transfer!.offsets.candidate_table).toBeGreaterThan(0)
    const offsets: Array<{ file: string; offset: number; bytes: number }> = []
    await transferMdFiles(choice, resumed.md_transfer!, publications, parent, stop.signal, async chunk => {
      const count = Buffer.from(chunk.data, "base64").byteLength
      expect(count).toBeLessThanOrEqual(128 * 1024)
      const receipt = await backend(tmp.path, "md_transfer_chunk", { md_chunk: chunk })
      expect(receipt.md_transfer!.offsets[chunk.file]).toBe(chunk.offset + count)
      offsets.push({ file: chunk.file, offset: chunk.offset, bytes: count })
    })
    expect(offsets.every(row => row.file !== "candidate_table" || row.offset >= resumed.md_transfer!.offsets.candidate_table)).toBe(true)
    const frozen = await backend(tmp.path, "md_transfer_commit", { md_choice_id: choice.id })
    expect(frozen.md_transfer!.state).toBe("frozen")
    expect(frozen.md_input!.md_system_ready).toBe(false)
    expect(frozen.md_input!.native_graph_qc_completed).toBe(false)
    expect(requests.every(route => route.includes("versionID=") && route.includes("directory="))).toBe(true)
    const again = await backend(tmp.path, "md_transfer_commit", { md_choice_id: choice.id })
    expect(again.md_input).toEqual(frozen.md_input)
    const cancelled = new AbortController()
    cancelled.abort()
    const count = requests.length
    await expect(transferMdFiles(choice, resumed.md_transfer!, publications, parent, cancelled.signal, async () => {
      throw Error("must never send")
    })).rejects.toThrow()
    expect(requests.length).toBe(count)
    // A changed version's actual bytes cannot be accepted, even if its URL and
    // metadata still claim the selected version. No freeze request follows.
    await Bun.write(path.join(tmp.path, "candidate_table"), Buffer.alloc(choice.files.candidate_table.bytes, 0))
    await expect(transferMdFiles(choice, frozen.md_transfer!, publications, parent, stop.signal, async () => {
      throw Error("frozen bytes must not be sent again")
    })).rejects.toThrow("bytes differ")
  } finally {
    server.stop(true)
    store.db.close()
  }
}, 60000)
