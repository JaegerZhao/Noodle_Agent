import { spawn } from 'node:child_process'
import { mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import net from 'node:net'
import { root, pythonExecutable } from './runtime.mjs'

async function freePort() {
  const server = net.createServer()
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const port = server.address().port
  await new Promise(resolve => server.close(resolve))
  return port
}
const location = await mkdtemp(join(tmpdir(), 'noodle-launcher-'))
const runRuntime = join(root, '.runtime', `launcher-check-${randomUUID()}`)
const python = pythonExecutable()
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(NOODLE_|DSH_|LLM_|MINERU_|DEEPSEEK_|DASHSCOPE_|EMBEDDING_|VISION_|DOUBAO_|ARK_)/i.test(key) && !/API_KEY|TOKEN|SECRET|PYTHONPATH|PYTHONHOME/i.test(key)))
const apiPort = await freePort(), harnessPort = await freePort()
Object.assign(env, { NOODLE_PYTHON: python, NOODLE_RUNTIME_DIR: runRuntime, NOODLE_LAUNCH_CWD: location,
  NOODLE_HARNESS_WORKSPACE: location, NOODLE_DATA_DIR: join(location, 'data'), NOODLE_EXPORT_DIR: join(location, 'exports'),
  NOODLE_API_PORT: String(apiPort), NOODLE_DSH_WEB_PORT: String(harnessPort), NOODLE_LLM: 'fixture', NOODLE_PARSER: 'fixture' })
const child = spawn(process.execPath, [join(root, 'scripts/start-local.mjs')], { cwd: root, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
let log = ''; child.stdout.on('data', chunk => { log += chunk }); child.stderr.on('data', chunk => { log += chunk })
async function waitFor(check, seconds = 75) {
  const deadline = Date.now() + seconds * 1000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Launcher exited: ${log.slice(-3000)}`)
    try { if (await check()) return } catch { /* not ready */ }
    await new Promise(resolve => setTimeout(resolve, 250))
  }
  throw new Error(`Startup timed out: ${log.slice(-3000)}`)
}
async function stop() {
  await new Promise((resolve, reject) => {
    const stopper = spawn(process.execPath, [join(root, 'scripts/stop-local.mjs')], { cwd: root, env, windowsHide: true, stdio: 'ignore' })
    stopper.once('error', reject); stopper.once('exit', resolve)
  })
}
try {
  const base = `http://127.0.0.1:${apiPort}`
  await waitFor(async () => (await fetch(`${base}/health`, { signal: AbortSignal.timeout(2000) })).ok)
  await waitFor(async () => (await fetch(`${base}/internal/v1/harness/sessions`, { signal: AbortSignal.timeout(2000) })).ok)
  const state = JSON.parse(await readFile(join(runRuntime, 'local/services.json'), 'utf8'))
  if (!state.api || !state.worker || !state.launcher) throw new Error('Missing managed process identity')
  await stop()
  const deadline = Date.now() + 15000
  while (child.exitCode === null && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 100))
  if (child.exitCode === null) throw new Error('Launcher did not stop')
  let alive = false
  try { alive = (await fetch(`${base}/health`, { signal: AbortSignal.timeout(1000) })).ok } catch { /* stopped */ }
  if (alive) throw new Error('API still running after stop')
  const report = { ok: true, platform: process.platform, isolated: true, apiWorkerHarness: true, stopped: true, artifacts: location }
  await writeFile(join(location, 'report.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report))
} finally { await stop(); if (child.exitCode === null) child.kill('SIGTERM') }
