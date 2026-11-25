import { expect, test } from "bun:test"
import path from "node:path"
import z from "zod"
import { tmpdir } from "../../../test/fixture/fixture"
import { mdCandidateChoice } from "./workflow-refinement-results"

test("host decodes an actual signed backend MD decision and its archived branch", async () => {
  await using tmp = await tmpdir({ git: true })
  const workspace = path.resolve(import.meta.dir, "../../../../../..")
  const script = `import json,sys
from pathlib import Path
from pytest import MonkeyPatch
from covscreen.workflow_store import load
from test_workflow_md_selection import research,ready,choose,exchange
with MonkeyPatch.context() as patch:
    context=research.__wrapped__(Path(sys.argv[1]),patch)
    value,observed,args=ready(context)
    public=choose(context,value,args)
    selected=load(context[1])["md_candidate"]
    released=exchange(context,"release_md_candidate","md_candidate_release",values={"reason":"Review another structural hypothesis while retaining the original decision"})
    print(json.dumps({"selected":selected,"public":public["md_candidate"],"archived":released["branches"][-1]["md_candidate"],"active":released["md_candidate"]}))
`
  const child = Bun.spawn([path.join(workspace, ".cache/refinement-validation-python310/bin/python"), "-B", "-c", script, tmp.path], {
    stdout: "pipe", stderr: "pipe", env: { ...process.env,
      PYTHONPATH: ["science/src", "science/tests"].map(name => path.join(workspace, name)).join(path.delimiter) },
  })
  const [stdout, stderr, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
  expect({ code, stderr }).toEqual({ code: 0, stderr: "" })
  const value = z.object({ selected: mdCandidateChoice, archived: mdCandidateChoice,
    public: z.object({ id: z.string(), state: z.literal("selected_asset_handoff_pending"), md_ready: z.literal(false),
      compute_submitted: z.literal(false) }).passthrough(), active: z.null() }).parse(JSON.parse(stdout))
  expect(value.archived).toEqual(value.selected)
  expect(value.public.id).toBe(value.selected.id)
  expect(value.selected.files.candidate_table.name).toBe("report/candidate-results.csv")
  expect(value.public).not.toHaveProperty("files")
  expect(value.public).not.toHaveProperty("parents")
}, 30000)
