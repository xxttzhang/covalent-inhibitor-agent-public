import path from "node:path"
import { discoveryProject, routeDefaults } from "../../openscience-custom/tooling/util/src/discovery-config"
const value = (name: string) => { const index = process.argv.indexOf(name); return index < 0 ? undefined : process.argv[index + 1] }
const directory = value("--directory")
const project = value("--project")
if (!directory || !project) throw Error("Usage: bun configure.ts --directory PROJECT_WORKTREE --project PROJECT_ID [--target TARGET_ID --name TARGET_NAME]")
const target = value("--target")
const name = value("--name")
if (target && !name) throw Error("A target name is required")
const config = discoveryProject.parse({ schema: "discovery-project-v1", project,
  target: target ? { id: target, name, label: name } : null,
  routes: Object.fromEntries(Object.entries(routeDefaults).map(([route, settings]) => [route, { ...settings, dataPath: `results/discovery/${route}.json`, inputPath: `.discovery/inbox/${route}.json` }])),
})
const destination = path.join(path.resolve(directory), ".discovery/project.json")
if (await Bun.file(destination).exists()) throw Error("Configuration already exists; edit it explicitly instead of overwriting")
await Bun.write(destination, JSON.stringify(config, null, 2))
console.log(JSON.stringify({ destination, project, target: config.target?.id ?? null }))
