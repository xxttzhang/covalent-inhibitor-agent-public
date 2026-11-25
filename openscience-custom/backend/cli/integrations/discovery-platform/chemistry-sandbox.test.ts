import { test, expect } from "bun:test"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import path from "node:path"
import os from "node:os"
import { chemistrySandbox } from "./chemistry-sandbox"

test("real chemistry sandbox denies project reads/writes and network access", async () => {
  const folder = await mkdtemp(path.join(os.tmpdir(), "chemistry-boundary-"))
  const filename = path.join(folder, "private.txt")
  await writeFile(filename, "test sentinel, no credentials")
  const program = `import json,socket\nresults={}\nfilename=${JSON.stringify(filename)}\nfor action in ('read','write','network'):\n try:\n  if action=='read': open(filename).read()\n  elif action=='write': open(filename,'w').write('changed')\n  else:\n   s=socket.socket();s.settimeout(1);s.connect(('127.0.0.1',9))\n  results[action]='allowed'\n except PermissionError: results[action]='denied'\n except Exception as e: results[action]=type(e).__name__\nprint(json.dumps(results))`
  const sandbox = await chemistrySandbox(program)
  try {
    const child = Bun.spawn([sandbox.planned.file, ...sandbox.planned.args], {
      stdin: "ignore", stdout: "pipe", stderr: "pipe", env: sandbox.env, cwd: sandbox.planned.temporary,
    })
    const output = await new Response(child.stdout).text()
    expect(await child.exited).toBe(0)
    expect(JSON.parse(output)).toEqual({ read: "denied", write: "denied", network: "denied" })
    expect(await Bun.file(filename).text()).toBe("test sentinel, no credentials")
  } finally { sandbox.cleanup(); await rm(folder, { recursive: true, force: true }) }
})
