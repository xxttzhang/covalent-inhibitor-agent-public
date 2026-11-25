import { expect, test } from 'bun:test'
import { mkdtemp, chmod, rm } from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import { approveCovalent, covalentExecution, covalentPreparer, covalentStartTools } from '../../src/session/covalent-approval'
import { PermissionNext } from '../../src/permission/next'
import { Instance } from '../../src/project/instance'
import { Session } from '../../src/session'
import { SessionTraceStore } from '../../src/session/trace-store'
import { Bus } from '../../src/bus'
import { tmpdir } from '../fixture/fixture'

test('mixed-version MCP fails closed without probing an execution tool', async () => {
  const counts = { execution: 0, preparation: 0 }
  const key = 'covalent-discovery_covalent_ligand_first_campaign'
  const tools = { [key]: async () => { counts.execution++; return {} } }
  expect(() => covalentPreparer(key, tools)).toThrow('upgrade MCP')
  expect(counts.execution).toBe(0)
  const args = { tool: 'covalent_ligand_first_campaign', arguments: { action: 'start' } }
  const updated = { ...tools, 'covalent-discovery_covalent_prepare_execution': async (request: unknown) => {
    counts.preparation++; return request
  } }
  expect(await covalentPreparer(key, updated)(args, {})).toEqual(args)
  expect(counts).toEqual({ execution: 0, preparation: 1 })
})

test('VS planning and observation cannot substitute for native execution approval', async () => {
  const actions = ['prepare_vs_input', 'read_vs_input', 'review_vs_environment', 'check_vs_runtime', 'start_vs', 'vs_status']
  const expected = [false, false, false, false, true, false]
  const key = 'covalent-discovery_covalent_ligand_design_step'
  expect(actions.map(action => covalentExecution(key, { action }))).toEqual(expected)
  const workspace = path.resolve(import.meta.dir, '../../../../..')
  const child = Bun.spawn([process.env.COVSCREEN_TEST_PYTHON ?? 'python3', '-B', '-c',
    'import json,sys; from covscreen.native_approval import needs_approval; print(json.dumps([needs_approval("covalent_ligand_design_step", {"action": a}) for a in json.loads(sys.argv[1])]))', JSON.stringify(actions)], {
    env: { ...process.env, PYTHONPATH: path.join(workspace, 'science/src') }, stdout: 'pipe', stderr: 'pipe',
  })
  expect(await child.exited).toBe(0)
  expect(JSON.parse(await new Response(child.stdout).text())).toEqual(expected)
})

test('workflow native graph admission requires approval only at its execution boundary', async () => {
  const actions = ['plan_md_admission', 'start_md_admission', 'md_admission_status', 'prepare_md_input',
    'plan_md_parameterization', 'start_md_parameterization', 'md_parameterization_status', 'plan_md_system_preparation', 'start_md_system_preparation', 'md_system_preparation_status', 'plan_md_short_qc', 'start_md_short_qc', 'md_short_qc_status', 'plan_md_equilibration', 'start_md_equilibration', 'md_equilibration_status', 'cancel_md_stage', 'release_md_stage',
    'plan_vs_prescreen', 'start_vs_prescreen', 'vs_prescreen_status', 'release_vs_prescreen',
    'prepare_vs_input', 'review_vs_environment', 'check_vs_runtime', 'start_vs', 'vs_status',
    'prepare_vs_refinement', 'read_lo_input', 'review_lo_environment', 'check_lo_runtime', 'start_lo', 'lo_status', 'evaluate_lo', 'report_lo']
  const key = 'covalent-discovery_covalent_library_research'
  const expected = [false, true, false, false, false, true, false, false, true, false, false, true, false, false, true, false, true, false, false, true, false, false, false, false, false, true, false,
    false, false, false, false, true, false, false, false]
  expect(actions.map(action => covalentExecution(key, { action }))).toEqual(expected)
  const workspace = path.resolve(import.meta.dir, '../../../../..')
  const child = Bun.spawn([(process.env.COVSCREEN_TEST_PYTHON ?? "python3"), '-c',
    'import json,sys; from covscreen.native_approval import needs_approval; print(json.dumps([needs_approval("covalent_library_research", {"action": a}) for a in json.loads(sys.argv[1])]))', JSON.stringify(actions)], {
    env: { ...process.env, PYTHONPATH: path.join(workspace, 'science/src') }, stdout: 'pipe', stderr: 'pipe',
  })
  expect(await child.exited).toBe(0)
  expect(JSON.parse(await new Response(child.stdout).text())).toEqual(expected)
})

test('Ligand graph and parameterization share the native approval boundary in host and MCP', async () => {
  const actions = ['select_md_candidate', 'release_md_candidate', 'check_md_graph_runtime', 'start_md_graph', 'md_graph_status',
    'plan_md_parameterization', 'start_md_parameterization', 'md_parameterization_status', 'release_md_parameterization_plan',
    'plan_md_system_preparation', 'start_md_system_preparation', 'md_system_preparation_status', 'release_md_system_preparation_plan',
    'plan_md_short_qc', 'start_md_short_qc', 'md_short_qc_status', 'release_md_short_qc_plan',
    'plan_md_equilibration', 'start_md_equilibration', 'md_equilibration_status', 'release_md_equilibration_plan']
  const expected = [false, false, false, true, false, false, true, false, false, false, true, false, false, false, true, false, false,
    false, true, false, false]
  const key = 'covalent-discovery_covalent_ligand_design_step'
  expect(actions.map(action => covalentExecution(key, { action }))).toEqual(expected)
  const workspace = path.resolve(import.meta.dir, '../../../../..')
  const child = Bun.spawn([(process.env.COVSCREEN_TEST_PYTHON ?? "python3"), '-B', '-c',
    'import json,sys; from covscreen.native_approval import needs_approval; print(json.dumps([needs_approval("covalent_ligand_design_step", {"action": a}) for a in json.loads(sys.argv[1])]))', JSON.stringify(actions)], {
    env: { ...process.env, PYTHONPATH: path.join(workspace, 'science/src') }, stdout: 'pipe', stderr: 'pipe',
  })
  expect(await child.exited).toBe(0)
  expect(JSON.parse(await new Response(child.stdout).text())).toEqual(expected)
})

test('every registered start tool enters the native approval bridge only for start', () => {
  expect([...covalentStartTools].sort()).toEqual([
    'covalent_library_first_admit_md_candidate',
    'covalent_library_first_analyze_openmm_replicas',
    'covalent_library_first_audit_openmm_trajectory',
    'covalent_library_first_build_md_report',
    'covalent_library_first_deliver_md_candidates',
    'covalent_library_first_equilibrate_openmm_system',
    'covalent_library_first_parameterize_adduct',
    'covalent_library_first_prepare_openmm_system',
    'covalent_library_first_prepare_receptor',
    'covalent_library_first_run_openmm_production',
    'covalent_library_first_run_openmm_short_qc',
    'covalent_library_receptor',
    'covalent_ligand_first_campaign',
    'covalent_predict_cysteine_sites',
  ])
  for (const name of covalentStartTools) {
    const key = `covalent-discovery_${name}`
    expect(covalentExecution(key, { action: 'plan' })).toBe(false)
    expect(covalentExecution(key, { action: 'status' })).toBe(false)
    expect(covalentExecution(key, { action: 'start' })).toBe(true)
  }
})

test('OpenScience and Python expose the same native-approval start allowlist', async () => {
  const workspace = path.resolve(import.meta.dir, '../../../../..')
  const python = (process.env.COVSCREEN_TEST_PYTHON ?? "python3")
  const child = Bun.spawn([python, '-c',
    'import json; from covscreen.native_approval import START_ACTION_TOOLS; print(json.dumps(sorted(START_ACTION_TOOLS)))'], {
    env: { ...process.env, PYTHONPATH: path.join(workspace, 'science/src') },
    stdout: 'pipe', stderr: 'pipe',
  })
  expect(await child.exited).toBe(0)
  expect(JSON.parse(await new Response(child.stdout).text())).toEqual([...covalentStartTools].sort())
})

test('native approval is mandatory, scoped and Python-verifiable', async () => {
  const folder = await mkdtemp(path.join(os.tmpdir(), 'covalent-approval-'))
  const before = process.env.COVALENT_APPROVAL_KEY_FILE
  try {
    const key = path.join(folder, 'key')
    await Bun.write(key, 'isolated-cross-language-test-key-123456789')
    await chmod(key, 0o600)
    process.env.COVALENT_APPROVAL_KEY_FILE = key
    const workspace = path.resolve(import.meta.dir, '../../../../..')
    const python = (process.env.COVSCREEN_TEST_PYTHON ?? "python3")
    const prepare = async (values: Record<string, unknown>) => {
      const child = Bun.spawn([python, '-c', `import sys,json\nfrom pathlib import Path\nfrom covscreen.approval_scope import prepare\nrequest=json.load(sys.stdin)\nprint(json.dumps({'structuredContent':prepare(Path(sys.argv[1]), request['tool'], request['arguments'])}))`, folder], {
        env: { ...process.env, PYTHONPATH: path.join(workspace, 'science/src') },
        stdin: new Blob([JSON.stringify(values)]), stdout: 'pipe', stderr: 'pipe',
      })
      expect(await child.exited).toBe(0)
      return JSON.parse(await new Response(child.stdout).text())
    }
    const name = 'covalent-discovery_covalent_submit_job'
    const args = { request_id: 'new-request-001', project_id: 'new-project', route: 'ligand-first',
      engine: 'rdkit-warhead-enumeration', parameters: { scaffold_smiles: 'CC', allowed_warheads: ['acrylamide'] },
      execution_mode: 'execute', resource_budget: { max_candidates: 2, max_runtime_minutes: 1 } }
    expect(covalentExecution(name, args)).toBe(true)
    expect(covalentExecution(name, { execution_mode: 'plan' })).toBe(false)
    await expect(approveCovalent(name, args, 's', 'm', async () => { throw new Error('rejected') }, prepare)).rejects.toThrow('rejected')
    const calls: unknown[] = []
    const approved = await approveCovalent(name, args, 's', 'm', async (data) => { calls.push(data); return 'permission_test' }, prepare)
    expect(calls).toHaveLength(1)
    const child = Bun.spawn([python, '-c', `import sys,json\nfrom pathlib import Path\nfrom covscreen.native_approval import authorize\nwith authorize(Path(sys.argv[1]), 'covalent_submit_job', json.load(sys.stdin)) as args:\n assert args['resource_budget']['max_candidates']==2\nprint('verified')`, folder], {
      env: { ...process.env, PYTHONPATH: path.join(workspace, 'science/src') },
      stdin: new Blob([JSON.stringify(approved)]), stdout: 'pipe', stderr: 'pipe',
    })
    expect(await child.exited).toBe(0)
    expect(await new Response(child.stdout).text()).toBe('verified\n')
  } finally {
    if (before === undefined) delete process.env.COVALENT_APPROVAL_KEY_FILE
    else process.env.COVALENT_APPROVAL_KEY_FILE = before
    await rm(folder, { recursive: true })
  }
})

test('real native card must settle and persist before signing; rejection and abort never sign', async () => {
  await using tmp = await tmpdir({ git: true })
  const before = process.env.COVALENT_APPROVAL_KEY_FILE
  const key = path.join(tmp.path, 'test-only.key')
  await Bun.write(key, 'isolated-native-card-test-key-123456789')
  await chmod(key, 0o600)
  process.env.COVALENT_APPROVAL_KEY_FILE = key
  try {
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const session = await Session.create({})
      for (const action of ['once', 'reject', 'abort'] as const) {
        const controller = new AbortController()
        const pending = Promise.withResolvers<PermissionNext.Request>()
        const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => pending.resolve(event.properties))
        const args = { execution_mode: 'execute', resource_budget: { max_candidates: 3, cpu_count: 4, gpu_count: 0 } }
        const result = approveCovalent('covalent_submit_job', args, session.id, 'msg_test', async (metadata) => {
          const id = `permission_covalent_${action}`
          await PermissionNext.ask({ id, sessionID: session.id, permission: 'remote_compute',
            patterns: [`covalent_submit_job:${metadata.execution_plan_sha256}`], always: [], metadata,
            ruleset: [{ permission: '*', pattern: '*', action: 'allow' }], signal: controller.signal })
          const record = (await SessionTraceStore.read(session.id)).approvals[id]
          expect(record.reply).toBe('once')
          expect(record.repliedAt).toBeGreaterThan(0)
          return id
        }, async () => ({ structuredContent: { status: 'NATIVE_APPROVAL_PREPARED', scope: args,
          scope_sha256: 'a'.repeat(64), compute_submitted: false } })).then(
            (value) => ({ value, error: undefined }), (error: unknown) => ({ value: undefined, error }))
        const card = await pending.promise
        unsubscribe()
        expect(card.metadata.execution_plan).toEqual(args)
        expect((await PermissionNext.list()).map((row) => row.id)).toContain(card.id)
        expect((await SessionTraceStore.read(session.id)).approvals[card.id]?.reply).toBeUndefined()
        if (action === 'abort') controller.abort()
        else await PermissionNext.reply({ requestID: card.id, reply: action })
        const completed = await result
        if (action === 'once') {
          expect(completed.error).toBeUndefined()
          const payload = JSON.parse(Buffer.from(completed.value!._native_approval.split('.')[0], 'base64url').toString())
          expect(payload.approval_request_id).toBe(card.id)
          expect(payload.session).toBe(session.id)
        } else {
          expect(completed.value).toBeUndefined()
          expect(completed.error).toBeDefined()
        }
        expect(await PermissionNext.list()).toEqual([])
      }
    } })
  } finally {
    if (before === undefined) delete process.env.COVALENT_APPROVAL_KEY_FILE
    else process.env.COVALENT_APPROVAL_KEY_FILE = before
  }
})

test('invalid real Python cancellation plans never reach native permission or signing', async () => {
  await using tmp = await tmpdir({ git: true })
  const previous = process.env.COVALENT_APPROVAL_KEY_FILE
  const key = path.join(tmp.path, 'cancel-test.key')
  await Bun.write(key, 'isolated-cancel-plan-test-key-123456789')
  await chmod(key, 0o600)
  process.env.COVALENT_APPROVAL_KEY_FILE = key
  const workspace = path.resolve(import.meta.dir, '../../../../..')
  try {
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const session = await Session.create({})
      const calls = { asked: 0, events: 0 }
      const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => {
        if (event.properties.sessionID === session.id) calls.events++
      })
      try {
        for (const mutation of ['external-hpc', 'external-inspire', 'protocol', 'terminal', 'identity']) {
          const result = await approveCovalent('covalent-discovery_covalent_cancel_job',
            { run_id: 'test-cancel-000001' }, session.id, 'msg_cancel_plan', async () => {
              calls.asked++
              throw new Error('Invalid plan reached native permission')
            }, async (values) => {
              // Reuse the Python synthetic queued-job fixture, but never launch
              // its worker. Exercise the production plan validator across the
              // language boundary; no approval or cancellation is performed.
              const child = Bun.spawn([(process.env.COVSCREEN_TEST_PYTHON ?? "python3"),
                '-c', `import json,sys
from pathlib import Path
from test_job_cancellation import queued
from covscreen.approval_scope import prepare
root=Path(sys.argv[1]); mutation=sys.argv[2]
registry,run=queued(root)
spec=registry.load_spec(run); result=registry.load_result(run)
if mutation.startswith('external'): spec.provenance['worker_backend']=mutation
elif mutation=='protocol': spec.provenance.pop('cancellation_protocol')
elif mutation=='terminal': result.state='COMPLETED'
else: result.project_id='different-project'
registry.write_spec(spec); registry.write_result(result)
before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
request=json.load(sys.stdin)
try:
    output={'structuredContent':prepare(root,request['tool'],request['arguments'])}
except ValueError as error:
    output={'isError':True,'content':[{'type':'text','text':str(error)}]}
assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
assert not (root/'started').exists()
print(json.dumps(output))`, path.join(tmp.path, mutation), mutation], {
                env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: [
                  path.join(workspace, 'science/src'),
                  path.join(workspace, 'science/tests'),
                ].join(path.delimiter) },
                stdin: new Blob([JSON.stringify(values)]), stdout: 'pipe', stderr: 'pipe',
              })
              const [code, stdout, stderr] = await Promise.all([
                child.exited, new Response(child.stdout).text(), new Response(child.stderr).text(),
              ])
              expect(stderr).toBe('')
              expect(code).toBe(0)
              const response = JSON.parse(stdout)
              expect(response.isError).toBe(true)
              const reason = mutation.startsWith('external') ? 'scheduler handle'
                : mutation === 'protocol' ? 'lacks the cooperative'
                : mutation === 'terminal' ? 'not cancellable' : 'identity mismatch'
              expect(response.content[0].text).toContain(reason)
              return response
            }).then(value => ({ value, error: undefined }), (error: unknown) => ({ value: undefined, error }))
          expect(result.value).toBeUndefined()
          expect(String(result.error)).toContain('no approval or execution')
        }
        expect(calls).toEqual({ asked: 0, events: 0 })
        expect(await PermissionNext.list()).toEqual([])
        expect((await SessionTraceStore.readForApproval(session.id)).approvals).toEqual({})
      } finally {
        unsubscribe()
        await Session.remove(session.id)
      }
    } })
  } finally {
    if (previous === undefined) delete process.env.COVALENT_APPROVAL_KEY_FILE
    else process.env.COVALENT_APPROVAL_KEY_FILE = previous
  }
})

test('Ligand-first pose, AF3 and CPU receptor preparation use the same native boundary on host and server', async () => {
  const actions = ['plan_pose', 'check_pose_runtime', 'start_pose', 'pose_status', 'deliver',
    'prepare_refinement_target', 'check_af3_runtime', 'start_af3', 'af3_status',
    'prepare_receptor_input', 'check_receptor_runtime', 'start_receptor_preparation', 'receptor_preparation_status',
    'evaluate_receptor', 'report_receptor', 'prepare_covdock_input', 'check_covdock_runtime',
    'review_covdock_environment', 'read_covdock_environment', 'start_covdock', 'covdock_status', 'evaluate_covdock', 'report_covdock']
  const expected = [false, false, true, false, false, false, false, true, false, false, false, true, false, false, false, false, false, false, false, true, false, false, false]
  expect(actions.map(action => covalentExecution('covalent-discovery_covalent_ligand_design_step', { action }))).toEqual(expected)
  const workspace = path.resolve(import.meta.dir, '../../../../..')
  const child = Bun.spawn([(process.env.COVSCREEN_TEST_PYTHON ?? "python3"), '-c',
    'import json,sys; from covscreen.native_approval import needs_approval; print(json.dumps([needs_approval("covalent_ligand_design_step", {"action": a}) for a in json.loads(sys.argv[1])]))', JSON.stringify(actions)], {
    env: { ...process.env, PYTHONPATH: path.join(workspace, 'science/src') }, stdout: 'pipe', stderr: 'pipe',
  })
  expect(await child.exited).toBe(0)
  expect(JSON.parse(await new Response(child.stdout).text())).toEqual(expected)
})
