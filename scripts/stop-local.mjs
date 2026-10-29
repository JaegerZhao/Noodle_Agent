import { join } from 'node:path'
import { root, state, removeState, stopProcess } from './runtime.mjs'

const services = state('services.json')
const harness = state('harness.json')
const entry = join(root, 'scripts/service-entry.py')
const candidates = [
  [harness?.child, join(root, 'node_modules/@deepseek-ai/dsh/lib/bin.js')],
  [harness?.supervisor, join(root, 'scripts/start-harness.mjs')],
  [services?.worker, entry], [services?.api, entry],
  [services?.launcher, [join(root, 'scripts/start-local.mjs'), join(root, 'scripts/start-next.mjs')]],
]
for (const [pid, marker] of candidates) {
  if (pid && await stopProcess(pid, Array.isArray(marker) ? marker : [marker])) console.log(`Stopped managed process ${pid}`)
}
if (services) removeState('services.json', 'launcher', services.launcher)
if (harness) removeState('harness.json', 'supervisor', harness.supervisor)
