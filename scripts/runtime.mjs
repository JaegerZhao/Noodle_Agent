import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import net from 'node:net'

export const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
export const runtime = resolve(process.env.NOODLE_RUNTIME_DIR || join(root, '.runtime'))
export const local = join(runtime, 'local')
export const cwd = resolve(process.env.NOODLE_LAUNCH_CWD || root)

export function pythonExecutable(env = process.env) {
  const candidates = [env.NOODLE_PYTHON,
    join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
    process.platform === 'win32' ? 'python' : 'python3', 'python'].filter(Boolean)
  for (const executable of candidates) {
    const result = spawnSync(executable, ['-c', 'import sys; print(sys.version_info.major, sys.version_info.minor)'], { encoding: 'utf8', windowsHide: true })
    if (result.status === 0) {
      const [major, minor] = result.stdout.trim().split(' ').map(Number)
      if (major === 3 && minor >= 12) return executable
    }
  }
  throw new Error('Python 3.12+ is required. Activate an environment or set NOODLE_PYTHON.')
}

function port(value, fallback) {
  const result = Number(value || fallback)
  if (!Number.isInteger(result) || result < 1 || result > 65535) throw new Error('Invalid service port')
  return result
}
export function environment() {
  const api = port(process.env.NOODLE_API_PORT, new URL(process.env.NOODLE_API_BASE_URL || 'http://127.0.0.1:8000').port || 8000)
  const harness = port(process.env.NOODLE_DSH_WEB_PORT || process.env.DSH_WEB_PORT,
    new URL(process.env.NOODLE_HARNESS_WEB_URL || 'http://127.0.0.1:3080').port || 3080)
  const mode = process.env.NOODLE_APP_MODE || 'next'
  if (mode !== 'next') throw new Error('Only the current next workspace is supported; historical runs retain their frozen code.')
  const data = resolve(cwd, process.env.NOODLE_NEXT_DATA_DIR || process.env.NOODLE_DATA_DIR || join(root, 'data/knowledge'))
  return { ...process.env,
    NOODLE_CONFIG_DIR: process.env.NOODLE_CONFIG_DIR || root,
    PYTHONPATH: [join(root, 'services/noodle/src'), process.env.PYTHONPATH].filter(Boolean).join(process.platform === 'win32' ? ';' : ':'),
    NOODLE_DATA_DIR: data,
    NOODLE_EXPORT_DIR: resolve(cwd, process.env.NOODLE_EXPORT_DIR || join(data, 'exports')),
    NOODLE_API_BASE_URL: `http://127.0.0.1:${api}`,
    NOODLE_APP_MODE: mode,
    NOODLE_API_PREFIX: '/internal/next',
    NOODLE_HARNESS_WEB_URL: `http://127.0.0.1:${harness}`,
    DSH_WEB_PORT: String(harness),
    NOODLE_RUNTIME_DIR: runtime,
  }
}

export async function checkPort(port) {
  const occupied = await new Promise(resolve => {
    const socket = net.connect({ host: '127.0.0.1', port })
    socket.setTimeout(1500)
    socket.once('connect', () => { socket.destroy(); resolve(true) })
    socket.once('error', () => { socket.destroy(); resolve(false) })
    socket.once('timeout', () => { socket.destroy(); resolve(false) })
  })
  if (occupied) throw new Error(`Port ${port} is already in use. Stop its owning service or change the configured port.`)
}

export async function run(executable, args, options = {}) {
  const child = spawn(executable, args, { cwd: root, env: environment(), stdio: 'inherit', windowsHide: true, ...options })
  return new Promise((resolve, reject) => {
    child.once('error', reject)
    child.once('exit', (code, signal) => signal ? reject(new Error(`Command stopped by ${signal}`)) : resolve(code ?? 1))
  })
}

export function state(name) {
  try { return JSON.parse(readFileSync(join(local, name), 'utf8').replace(/^\uFEFF/, '')) } catch { return null }
}
export function removeState(name, field, id) {
  if (state(name)?.[field] === id) rmSync(join(local, name), { force: true })
}

export function ownsProcess(pid, markers) {
  if (!Number.isSafeInteger(pid) || pid < 1 || pid === process.pid) return false
  let command
  if (process.platform === 'win32') {
    const query = `(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}").CommandLine`
    const result = spawnSync('powershell.exe', ['-NoProfile', '-Command', query], { encoding: 'utf8', windowsHide: true })
    if (result.status !== 0) return false
    command = result.stdout
  } else if (existsSync(`/proc/${pid}/cmdline`)) {
    command = readFileSync(`/proc/${pid}/cmdline`, 'utf8').replaceAll('\0', ' ')
  } else {
    const result = spawnSync('ps', ['-p', String(pid), '-o', 'command='], { encoding: 'utf8' })
    if (result.status !== 0) return false
    command = result.stdout
  }
  const normalized = command.replaceAll('\\', '/').toLowerCase()
  return markers.some(marker => normalized.includes(marker.replaceAll('\\', '/').toLowerCase()))
}

export async function stopProcess(pid, markers) {
  if (!ownsProcess(pid, markers)) return false
  if (process.platform === 'win32') {
    const result = spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
    return result.status === 0
  }
  try { process.kill(pid, 'SIGTERM') } catch { return false }
  for (let attempt = 0; attempt < 40; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100))
    try { process.kill(pid, 0) } catch { return true }
  }
  if (ownsProcess(pid, markers)) { try { process.kill(pid, 'SIGKILL') } catch { /* already exited */ } }
  return true
}

export function ensureRuntime() { mkdirSync(local, { recursive: true }) }
