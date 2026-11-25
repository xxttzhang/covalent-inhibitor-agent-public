import { For, Match, Show, Switch, type JSX } from "solid-js"
import { DateTime } from "luxon"
import { AppHeader } from "@/atlas/AppHeader"
import {
  IconActivity,
  IconAtom,
  IconChevronRight,
  IconFlask,
  IconFolderAdd,
  IconPin,
  IconPinFilled,
  IconPlus,
  IconSearch,
  IconSettings,
  IconTrash,
  IconX,
} from "@/atlas/shared/Icon"
import { Wordmark } from "@/atlas/Wordmark"
import { projectName, type LauncherState, type PreparedProject } from "./home-projects"
import { preloadSession } from "./session-loader"
import "./home-workbench.css"

export type HomeProject = PreparedProject & {
  sessions?: number
}

export function ProjectsWorkbench(props: {
  state: LauncherState
  projects: HomeProject[]
  totalProjects: number
  query: string
  refreshing?: boolean
  accessory?: JSX.Element
  notice?: JSX.Element
  serverName: string
  serverStatus: "checking" | "healthy" | "error"
  onQuery: (query: string) => void
  onOpen: (project: HomeProject) => void
  onPin: (project: HomeProject) => void
  onRemove: (project: HomeProject) => void
  onCreate: () => void
  onImport: () => void
  onRetry: () => void
  onSettings: () => void
  onServer: () => void
}): JSX.Element {
  const recent = () => props.state === "recent"
  let input: HTMLInputElement | undefined

  const clearSearch = () => {
    props.onQuery("")
    input?.focus()
  }

  const countLabel = () => {
    const total = props.totalProjects
    const noun = total === 1 ? "project" : "projects"
    if (props.query.trim()) return `${props.projects.length} of ${total} ${noun}`
    return `${total} ${noun}`
  }

  const editedLabel = (project: HomeProject) => {
    const relative = DateTime.fromMillis(project.updatedAt).toRelative() ?? "recently"
    return `Edited ${relative}`
  }

  return (
    <div class="science-home__view">
      <AppHeader class="science-home__bar">
        <Wordmark size="sm" />
        <span class="science-home__spacer" />

        <div class="science-home__bar-actions">
          {props.accessory}
          <button
            class="science-home__server"
            type="button"
            aria-label={props.serverName}
            title={`Server · ${props.serverName}`}
            onClick={props.onServer}
          >
            <span class="science-home__server-dot" data-status={props.serverStatus} aria-hidden="true" />
            <span>{props.serverName}</span>
          </button>
          <span class="science-home__bar-divider" aria-hidden="true" />
          <button class="science-home__icon" type="button" aria-label="Settings" onClick={props.onSettings}>
            <IconSettings size={15} strokeWidth={1.5} />
          </button>
        </div>
      </AppHeader>

      {props.notice}

      <main class="atlas-scroll science-home__main">
        <div class="science-home__content">
          <header class="science-home__heading">
            <div class="science-home__heading-copy">
              <div class="science-home__eyebrow">
                <span aria-hidden="true">
                  <IconAtom size={12} strokeWidth={1.5} />
                </span>
                COVALENT R&amp;D · PROJECT COMMAND CENTER
              </div>
              <div class="science-home__title">
                <h1 id="science-home-projects-title">Covalent Discovery</h1>
                <Show when={recent() && props.refreshing}>
                  <span class="science-home__refreshing" role="status">
                    Syncing…
                  </span>
                </Show>
              </div>
              <p>面向共价抑制剂发现的多智能体研发工作台。</p>
              <div class="science-home__workflow" aria-label="Discovery workflow">
                <span>结构准备</span>
                <i aria-hidden="true" />
                <span>分子设计</span>
                <i aria-hidden="true" />
                <span>证据审计</span>
              </div>
            </div>
            <div class="science-home__heading-side">
              <div class="science-home__overview" aria-label="Workspace overview">
                <span>
                  <strong>{String(props.totalProjects).padStart(2, "0")}</strong>
                  <small>ACTIVE PROJECTS</small>
                </span>
                <span>
                  <IconActivity size={14} strokeWidth={1.5} />
                  <strong data-status={props.serverStatus}>
                    {props.serverStatus === "healthy"
                      ? "ONLINE"
                      : props.serverStatus === "checking"
                        ? "SYNCING"
                        : "OFFLINE"}
                  </strong>
                  <small>LOCAL RUNTIME</small>
                </span>
              </div>
              <div class="science-home__heading-actions">
                <button
                  class="science-home__button"
                  type="button"
                  aria-label="Import existing folder"
                  onClick={props.onImport}
                >
                  <IconFolderAdd size={15} strokeWidth={1.45} />
                  Import folder
                </button>
                <button
                  class="science-home__button science-home__button--primary"
                  type="button"
                  aria-label="New project"
                  onClick={props.onCreate}
                >
                  <IconPlus size={15} strokeWidth={1.5} />
                  New project
                </button>
              </div>
            </div>
          </header>

          <Show when={recent()}>
            <div class="science-home__section-heading">
              <div>
                <span>PROJECT WORKSPACES</span>
                <strong>继续最近的研究</strong>
              </div>
              <p>打开项目以恢复会话、文件和候选档案。</p>
            </div>
            <div class="science-home__toolbar" aria-label="Project controls">
              <label class="science-home__search" for="science-home-project-search">
                <IconSearch size={16} strokeWidth={1.45} />
                <input
                  ref={input}
                  id="science-home-project-search"
                  type="search"
                  aria-label="Search projects"
                  aria-controls="science-home-project-list"
                  value={props.query}
                  placeholder="Search projects by name, folder, or ID"
                  onInput={(event) => props.onQuery(event.currentTarget.value)}
                  onKeyDown={(event) => {
                    if (event.key !== "Escape") return
                    props.onQuery("")
                  }}
                />
                <Show when={props.query}>
                  <button type="button" aria-label="Clear search" onClick={clearSearch}>
                    <IconX size={13} strokeWidth={1.55} />
                  </button>
                </Show>
              </label>
              <span class="science-home__results-summary sr-only" role="status" aria-live="polite">
                {countLabel()}
              </span>
            </div>
          </Show>

          <Switch>
            <Match when={props.state === "loading"}>
              <section class="science-home__state" role="status" aria-live="polite">
                <span class="science-home__spinner" aria-hidden="true" />
                <div>
                  <strong>Loading projects…</strong>
                  <span>Reading projects from this server.</span>
                </div>
              </section>
            </Match>

            <Match when={props.state === "error"}>
              <section class="science-home__state science-home__state--error" role="alert">
                <div>
                  <strong>Local workspace unavailable</strong>
                  <span>Check the selected server, then try the connection again.</span>
                </div>
                <button class="science-home__button" type="button" onClick={props.onRetry}>
                  Try again
                </button>
              </section>
            </Match>

            <Match when={props.state === "empty"}>
              <section class="science-home__state science-home__state--empty">
                <div>
                  <strong>No projects yet</strong>
                  <span>Start a new project or import a folder to bring your research together.</span>
                </div>
              </section>
            </Match>

            <Match when={props.state === "recent"}>
              <Show
                when={props.projects.length > 0}
                fallback={
                  <section
                    id="science-home-project-list"
                    class="science-home__state science-home__state--empty"
                    aria-live="polite"
                  >
                    <div>
                      <strong>No matching projects</strong>
                      <span>No projects match “{props.query.trim()}”. Try another search or clear this one.</span>
                    </div>
                    <button class="science-home__button" type="button" onClick={clearSearch}>
                      Clear search
                    </button>
                  </section>
                }
              >
                <ul
                  id="science-home-project-list"
                  class="science-home__projects"
                  aria-labelledby="science-home-projects-title"
                >
                  <For each={props.projects}>
                    {(project) => (
                      <li class="science-home__project-row" data-pinned={project.pinned ? "true" : undefined}>
                        <button
                          class="science-home__project"
                          type="button"
                          data-project={project.id}
                          title={`Open ${projectName(project)}`}
                          onPointerEnter={preloadSession}
                          onFocus={preloadSession}
                          onClick={() => props.onOpen(project)}
                        >
                          <span class="science-home__project-mark" aria-hidden="true">
                            <IconFlask size={16} strokeWidth={1.45} />
                          </span>
                          <span class="science-home__project-copy">
                            <strong>{projectName(project)}</strong>
                            <span class="science-home__project-meta">
                              <Show when={project.sessions !== undefined}>
                                <span class="science-home__sessions">
                                  {project.sessions} {project.sessions === 1 ? "session" : "sessions"}
                                </span>
                                <span class="science-home__meta-separator" aria-hidden="true">
                                  ·
                                </span>
                              </Show>
                              <time datetime={DateTime.fromMillis(project.updatedAt).toISO() ?? undefined}>
                                {editedLabel(project)}
                              </time>
                            </span>
                          </span>
                          <span class="science-home__project-open" aria-hidden="true">
                            <IconChevronRight size={14} strokeWidth={1.5} />
                          </span>
                        </button>
                        <div class="science-home__project-actions">
                          <button
                            class="science-home__project-action science-home__project-pin"
                            type="button"
                            aria-label={`${project.pinned ? "Unpin" : "Pin"} ${projectName(project)}`}
                            aria-pressed={project.pinned}
                            title={project.pinned ? "Unpin project" : "Pin project"}
                            onClick={() => props.onPin(project)}
                          >
                            <Show when={project.pinned} fallback={<IconPin size={15} strokeWidth={1.4} />}>
                              <IconPinFilled size={15} strokeWidth={1.4} />
                            </Show>
                          </button>
                          <button
                            class="science-home__project-action science-home__project-remove"
                            type="button"
                            aria-label={`Remove ${projectName(project)} from home`}
                            title="Remove project from home"
                            onClick={() => props.onRemove(project)}
                          >
                            <IconTrash size={14} strokeWidth={1.4} />
                          </button>
                        </div>
                      </li>
                    )}
                  </For>
                </ul>
              </Show>
            </Match>
          </Switch>
        </div>
      </main>
    </div>
  )
}
