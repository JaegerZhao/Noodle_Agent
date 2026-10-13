import { healProfilesModuleFallback } from '@deepseek-ai/dsh-app-boot'
import { mkdir, rm, writeFile, readFile } from 'node:fs/promises'
import { spawn } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

process.env.DSH_WEB_PORT ||= process.env.NOODLE_DSH_WEB_PORT || '3080'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const runtimeDir = resolve(process.env.NOODLE_RUNTIME_DIR ?? join(root, '.runtime'))
const localRuntimeDir = join(runtimeDir, 'local')
const harnessStatePath = join(localRuntimeDir, 'harness.json')
const dshHome = process.env.DSH_HOME ?? join(runtimeDir, 'dsh-home')
const pluginPath = pathToFileURL(join(root, 'dist', 'index.js')).href
const patchPath = join(runtimeDir, 'noodle.overlay.yml')
await mkdir(localRuntimeDir, { recursive: true })
// Profile roots may live outside the checkout. Use Harness's supported
// fallback linker with all installed providers, including development bundles.
const packageManifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'))
await mkdir(join(root, '.runtime'), { recursive: true })
const providerManifest = join(root, '.runtime', `profile-dependencies-${process.pid}.json`)
await writeFile(providerManifest, JSON.stringify({ dependencies: {
  ...packageManifest.dependencies, ...packageManifest.devDependencies,
} }))
try { healProfilesModuleFallback(providerManifest, dshHome) } finally { await rm(providerManifest, { force: true }) }
await writeFile(patchPath, `- insert:\n    - id: noodle-domain-agent\n      name: '${pluginPath.replaceAll("'", "''")}'\n      config:\n        baseUrl: !!js process.env.NOODLE_API_BASE_URL || 'http://127.0.0.1:8000'\n        apiPrefix: !!js process.env.NOODLE_API_PREFIX || '/internal/v1'\n        timeoutMs: !!js Number(process.env.NOODLE_API_TIMEOUT_MS || 120000)\n        maxBodyChars: 12000\n        workspacePath: '${(process.env.NOODLE_HARNESS_WORKSPACE ?? root).replaceAll("'", "''")}'\n        workspaceTitle: Noodle Agent\n`, 'utf8')

const dshBin = join(root, 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')
const dshArgs = [dshBin, 'web', '--patch', patchPath, '--no-open']
if (process.env.DSH_WEB_HOST) dshArgs.push('--host', process.env.DSH_WEB_HOST)
if (process.env.DSH_WEB_PORT) dshArgs.push('--port', process.env.DSH_WEB_PORT)
const child = spawn(process.execPath, dshArgs, {
  cwd: process.env.NOODLE_LAUNCH_CWD ?? root,
  env: { ...process.env, DSH_HOME: dshHome },
  stdio: 'inherit',
})
await writeFile(harnessStatePath, JSON.stringify({
  supervisor: process.pid,
  child: child.pid,
  started_at: new Date().toISOString(),
  projectRoot: root,
}, null, 2), 'utf8')

child.on('exit', async code => {
  await rm(harnessStatePath, { force: true }).catch(() => {})
  process.exit(code ?? 1)
})
let stopping = false
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    if (stopping) return
    stopping = true
    child.kill(signal)
    const forceTimer = setTimeout(() => child.kill('SIGKILL'), 5000)
    forceTimer.unref()
  })
}
