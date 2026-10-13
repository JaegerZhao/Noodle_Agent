import { join } from 'node:path'
import { mkdirSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { root, runtime, cwd, environment, pythonExecutable, run } from './runtime.mjs'

const task = process.argv[2]
const env = environment()
const python = pythonExecutable(env)
mkdirSync(runtime, { recursive: true })
const args = {
  setup: ['-m', 'pip', 'install', '-e', './services/noodle[dev]'],
  api: [join(root, 'scripts/service-entry.py'), 'api', '--port', new URL(env.NOODLE_API_BASE_URL).port],
  worker: [join(root, 'scripts/service-entry.py'), 'worker'],
  test: ['-m', 'pytest', './services/noodle/tests', '--basetemp', join(runtime, `pytest-${randomUUID()}`), '-p', 'no:cacheprovider'],
  lint: ['-m', 'ruff', 'check', './services/noodle/src', './services/noodle/tests'],
  delivery: [join(root, 'scripts/delivery-check.py')],
  upgrade: ['-m', 'noodle_agent.cli', 'db-upgrade'],
  status: ['-m', 'noodle_agent.cli', 'db-status'],
}[task]
if (!args) throw new Error('Unknown Python task')
process.exitCode = await run(python, args, { env, cwd: ['api', 'worker', 'upgrade', 'status'].includes(task) ? cwd : root })
