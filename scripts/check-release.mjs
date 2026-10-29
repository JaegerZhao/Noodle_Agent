// Inspect the prospective source snapshot, never print credential values.
import { execFileSync } from 'node:child_process'
import { readFileSync, statSync, lstatSync } from 'node:fs'
import { resolve } from 'node:path'
import { isDeepStrictEqual } from 'node:util'

const root = resolve(import.meta.dirname, '..')
const deleted = new Set(execFileSync('git',['ls-files','-z','--deleted'],{cwd:root}).toString().split('\0').filter(Boolean))
const paths = [...new Set(execFileSync('git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], { cwd: root }).toString().split('\0').filter(path=>path&&!deleted.has(path)))]
const issues = []
const secretPatterns = [
  /\bsk-[A-Za-z0-9_-]{24,}\b/,
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
  /\bgh[pousr]_[A-Za-z0-9]{30,}\b/,
  /\bAKIA[0-9A-Z]{16}\b/,
]
for (const path of paths) {
  if (/(^|\/)(?:node_modules|\.runtime|\.local-only|\.venv|__pycache__|\.noodle)(\/|$)/.test(path)
      || /(^|\/)\.env(?:\..*)?$/.test(path) && !path.endsWith('.example')
      || /\.(?:db(?:-wal|-shm)?|sqlite3?(?:-wal|-shm)?|pem|key|p12|pfx|log|pdf|zip)$/.test(path)
      || /^data\//.test(path) && path !== 'data/.gitkeep') {
    issues.push(`${path}: private/runtime artifact in source snapshot`)
    continue
  }
  const absolute = resolve(root, path)
  let size
  try { if (lstatSync(absolute).isSymbolicLink()) issues.push(`${path}: symlink requires review`); size = statSync(absolute).size } catch { issues.push(`${path}: tracked file missing`); continue }
  if (size > 10 * 1024 * 1024) issues.push(`${path}: exceeds 10 MiB source-file limit`)
  if (!/\.(?:py|ts|js|mjs|json|ya?ml|toml|md|ps1|example)$/.test(path)) continue
  const lines = readFileSync(absolute, 'utf8').split(/\r?\n/)
  lines.forEach((line, index) => {
    if (secretPatterns.some(pattern => pattern.test(line))) issues.push(`${path}:${index + 1}: possible credential (value redacted)`)
  })
}
const pkg = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'))
const lock = JSON.parse(readFileSync(resolve(root, 'package-lock.json'), 'utf8')).packages['']
for (const section of ['dependencies', 'devDependencies']) {
  if (!isDeepStrictEqual(pkg[section] || {}, lock[section] || {})) issues.push(`package-lock.json: ${section} differs from package.json`)
}
console.log(JSON.stringify({ ok: issues.length === 0, files: paths.length, issues, scope: 'working source snapshot; heuristic scan, not a full Git-history security audit' }, null, 2))
process.exitCode = issues.length ? 1 : 0
