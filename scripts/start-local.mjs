import { spawn } from 'node:child_process'
import { openSync, closeSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { root, local, cwd, environment, pythonExecutable, ensureRuntime, checkPort, run, removeState, stopProcess } from './runtime.mjs'

const env = environment()
const python = pythonExecutable(env)
const entry = join(root, 'scripts/service-entry.py')
const harnessEntry = join(root, 'scripts/start-harness.mjs')
const processes = []
let shuttingDown = false

async function shutdown() {
  if (shuttingDown) return
  shuttingDown = true
  for (const { child, marker } of processes.reverse()) await stopProcess(child.pid, [marker])
  removeState('services.json', 'launcher', process.pid)
}
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => { shutdown().finally(() => process.exit(0)) })

function background(role) {
  const out = openSync(join(local, `${role}.stdout.log`), 'w')
  const err = openSync(join(local, `${role}.stderr.log`), 'w')
  const args = [entry, role]
  if (role === 'api') args.push('--port', new URL(env.NOODLE_API_BASE_URL).port)
  const child = spawn(python, args, { cwd, env, stdio: ['ignore', out, err], windowsHide: true })
  closeSync(out); closeSync(err)
  processes.push({ child, marker: entry })
  child.once('error', error => { console.error(`${role}: ${error.message}`); shutdown().finally(() => process.exit(1)) })
  child.once('exit', () => { if (!shuttingDown) { console.error(`${role} exited; inspect ${local}`); shutdown().finally(() => process.exit(1)) } })
  return child
}

try {
  await checkPort(Number(new URL(env.NOODLE_API_BASE_URL).port))
  await checkPort(Number(env.DSH_WEB_PORT))
  ensureRuntime()
  const build = await run(process.execPath, [join(root, 'node_modules/typescript/bin/tsc'), '-p', join(root, 'tsconfig.json')], { env })
  if (build) throw new Error('TypeScript build failed')
  const api = background('api')
  const deadline = Date.now() + 60000
  let healthy = false
  while (Date.now() < deadline && !shuttingDown) {
    if (api.exitCode !== null) break
    try {
      const response = await fetch(`${env.NOODLE_API_BASE_URL}/health`, { signal: AbortSignal.timeout(2000) })
      const body = await response.json()
      if (response.ok && body.api_surface === 'internal-v1') { healthy = true; break }
    } catch { /* startup in progress */ }
    await new Promise(resolve => setTimeout(resolve, 250))
  }
  if (!healthy) throw new Error(`API startup failed. See ${local}`)
  const worker = background('worker')
  writeFileSync(join(local, 'services.json'), JSON.stringify({ api: api.pid, worker: worker.pid, launcher: process.pid, projectRoot: root, started_at: new Date().toISOString() }, null, 2))
  console.log(`Noodle Workbench: ${env.NOODLE_API_BASE_URL}/app/`)
  console.log(`Noodle Harness: ${env.NOODLE_HARNESS_WEB_URL}`)
  const harness = spawn(process.execPath, [harnessEntry], { cwd: root, env, stdio: 'inherit', windowsHide: true })
  processes.push({ child: harness, marker: harnessEntry })
  const exitCode = await new Promise((resolve, reject) => { harness.once('error', reject); harness.once('exit', code => resolve(code ?? 1)) })
  if (!shuttingDown) process.exitCode = exitCode
} catch (error) {
  console.error(error.message)
  process.exitCode = 1
} finally { await shutdown() }
