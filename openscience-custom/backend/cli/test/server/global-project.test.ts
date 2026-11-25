import { afterEach, describe, expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { Global } from "../../src/global"
import { ManagedProject } from "../../src/project/managed"
import { Project } from "../../src/project/project"
import { GlobalRoutes } from "../../src/server/routes/global"
import { Storage } from "../../src/storage/storage"
import { $ } from "bun"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { SessionFilesystem } from "../../src/session/filesystem"

const created: Project.Info[] = []

afterEach(async () => {
  const projects = created.splice(0)
  await Promise.all(
    projects.map(async (project) => {
      await Storage.remove(["managed_project", project.id]).catch(() => undefined)
      await Storage.remove(["project", project.id]).catch(() => undefined)
      await Storage.remove(["project_filesystem", project.id]).catch(() => undefined)
      await fs.rm(project.worktree, { recursive: true, force: true })
    }),
  )
})

async function create(body: unknown) {
  return GlobalRoutes().request("/project", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  })
}

describe("global.project.create", () => {
  test("keeps managed projects and their sessions isolated from an ancestor Git repository", async () => {
    const parent = await fs.realpath(Global.Path.data)
    const git = path.join(parent, ".git")
    expect(await Bun.file(path.join(git, "HEAD")).exists()).toBe(false)
    await $`git init ${parent}`.quiet()
    try {
      const first = Project.Info.parse(await (await create({ name: "Inside repository" })).json())
      expect(path.dirname(first.worktree)).toBe(path.join(parent, "projects"))
      created.push(first)
      const second = Project.Info.parse(await (await create({ name: "Inside repository" })).json())
      expect(path.dirname(second.worktree)).toBe(path.join(parent, "projects"))
      created.push(second)
      expect(first.id).not.toBe(second.id)
      const nested = path.join(first.worktree, "inputs")
      await fs.mkdir(nested)
      expect((await Project.fromDirectory(nested)).project.id).toBe(first.id)
      await Instance.provide({
        directory: first.worktree,
        fn: async () => {
          try {
            const session = await Session.create({})
            const grants = await SessionFilesystem.list(session.id)
            expect(grants.some((grant) => grant.path === parent)).toBe(false)
            expect(grants.some((grant) => grant.source === "workspace")).toBe(true)
            await Session.remove(session.id)
          } finally {
            await Instance.dispose()
          }
        },
      })
    } finally {
      await fs.rm(git, { recursive: true, force: true })
    }
  })

  test("creates a named project under an opaque server-owned root", async () => {
    const response = await create({ name: "  Protein   folding  " })

    expect(response.status).toBe(201)
    const project = Project.Info.parse(await response.json())
    created.push(project)

    expect(project.id).toStartWith("prj_")
    expect(project.name).toBe("Protein folding")
    expect(path.dirname(project.worktree)).toBe(await fs.realpath(path.join(Global.Path.data, "projects")))
    expect(path.basename(project.worktree)).not.toContain("Protein")
    expect((await fs.stat(project.worktree)).isDirectory()).toBe(true)
    expect(ManagedProject.Info.parse(await Storage.read(["managed_project", project.id]))).toEqual({
      version: 1,
      projectID: project.id,
      directory: await fs.realpath(project.worktree),
      time: {
        created: expect.any(Number),
      },
    })
  })

  test("keeps repeated display names as distinct projects", async () => {
    const first = Project.Info.parse(await (await create({ name: "Cell atlas" })).json())
    const second = Project.Info.parse(await (await create({ name: "Cell atlas" })).json())
    created.push(first, second)

    expect(second.id).not.toBe(first.id)
    expect(second.worktree).not.toBe(first.worktree)
    expect(second.name).toBe(first.name)
  })

  test("attaches explicitly selected source folders as durable project access", async () => {
    const source = await fs.mkdtemp(path.join(os.tmpdir(), "openscience-project-source-"))
    const response = await create({
      name: "Cell atlas",
      sources: [{ path: source, access: "write" }],
    })
    const project = Project.Info.parse(await response.json())
    created.push(project)

    expect(response.status).toBe(201)
    expect(await Storage.read(["project_filesystem", project.id])).toMatchObject({
      version: 1,
      projectID: project.id,
      grants: [
        {
          path: await fs.realpath(source),
          access: "write",
          scope: "project",
          source: "api",
        },
      ],
    })
    await fs.rm(source, { recursive: true, force: true })
  })

  test("rejects unsafe names and client-selected host paths", async () => {
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), "openscience-project-create-"))
    const bodies = [
      { name: "   " },
      { name: "../outside" },
      { name: "unsafe\u0000name" },
      { name: "Imported", directory: outside },
    ]

    const responses = await Promise.all(bodies.map(create))

    expect(responses.map((response) => response.status)).toEqual([400, 400, 400, 400])
    expect(await fs.readdir(outside)).toEqual([])
    await fs.rm(outside, { recursive: true, force: true })
  })

  test("rolls back the marker, project record, and exact directory when creation fails", async () => {
    const before = new Set(await fs.readdir(path.join(Global.Path.data, "projects")).catch(() => []))
    const failure = await ManagedProject.create("Rollback checkpoint", async (created) => {
      throw Object.assign(new Error("checkpoint failed"), { project: created })
    })
      .then(() => {
        throw new Error("Expected managed project creation to fail")
      })
      .catch((error) => error as Error & { project: Project.Info })
    const project = failure.project

    expect(failure).toBeInstanceOf(Error)
    expect(failure.message).toBe("checkpoint failed")
    expect(project.id).toStartWith("prj_")
    expect(await Bun.file(project.worktree).exists()).toBe(false)
    await expect(Storage.read(["project", project.id])).rejects.toBeInstanceOf(Storage.NotFoundError)
    await expect(Storage.read(["managed_project", project.id])).rejects.toBeInstanceOf(Storage.NotFoundError)
    const after = new Set(await fs.readdir(path.join(Global.Path.data, "projects")).catch(() => []))
    expect(after).toEqual(before)
  })
})
