import { access, mkdir } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { chromium } from 'playwright-core'

const candidates = [
  process.env.EDGE_EXECUTABLE,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].filter(Boolean)

let executablePath
for (const candidate of candidates) {
  try { await access(candidate); executablePath = candidate; break } catch { /* next */ }
}
if (!executablePath) throw new Error('Microsoft Edge was not found; set EDGE_EXECUTABLE')

const root = resolve(import.meta.dirname, '..')
const outputDir = join(root, '.runtime')
await mkdir(outputDir, { recursive: true })
const consoleErrors = []
const browser = await chromium.launch({ executablePath, headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1500, height: 960 } })
  await page.route('**/internal/v1/harness/sessions/*/history*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }),
  }))
  await page.route('**/internal/v1/harness/sessions', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ sessionId: 'synthesis-smoke', title: 'Smoke', origin: 'root', running: false }] }),
  }))
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()) })
  page.on('pageerror', error => consoleErrors.push(`pageerror: ${error.message}`))
  await page.goto('http://127.0.0.1:8000/app/', { waitUntil: 'domcontentloaded' })
  await page.locator('#open-syntheses').click()
  const third = page.locator('.synthesis-card', { hasText: '大语言模型知识体系' })
  await third.waitFor({ timeout: 10_000 })

  await page.locator('#create-synthesis').click()
  await page.locator('#synthesis-editor[open]').waitFor()
  await page.waitForFunction(() => document.querySelectorAll('#concept-registry-results .concept-registry-row').length > 0)
  const createFixture = {
    registryRows: await page.locator('#concept-registry-results .concept-registry-row').count(),
    defaultGroups: await page.locator('#synthesis-draft-tree .synthesis-draft-group').count(),
  }
  await page.locator('#concept-registry-search').fill('Transformer')
  await page.waitForFunction(() => {
    const count = document.querySelectorAll('#concept-registry-results .concept-registry-row').length
    return count > 1 && count < 80
  })
  createFixture.serverSearchRows = await page.locator('#concept-registry-results .concept-registry-row').count()
  const registryRows = page.locator('#concept-registry-results .concept-registry-row')
  await registryRows.nth(0).getByRole('button', { name: '设为源' }).click()
  await registryRows.nth(1).getByRole('button', { name: '设为目标' }).click()
  createFixture.mergeSourceSelected = Boolean(await page.locator('#merge-source-concept').getAttribute('data-concept-id'))
  createFixture.mergeTargetSelected = Boolean(await page.locator('#merge-target-concept').getAttribute('data-concept-id'))
  let previewRequests = 0
  let mutationRequests = 0
  let previewDialog = ''
  await page.route('**/internal/v1/concepts/merge-preview', route => {
    previewRequests += 1
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ source_title: 'Source', target_title: 'Target', relation: 'REQUIRES_HUMAN_JUDGMENT', scope_after: { includes: ['CNN'] }, modules: [{ title: 'Overview', action: 'COMBINE', identical_blocks: 1 }] }) })
  })
  await page.route('**/internal/v1/concepts/merge', route => { mutationRequests += 1; return route.abort() })
  page.once('dialog', async dialog => { previewDialog = dialog.message(); await dialog.dismiss() })
  await page.locator('#merge-rationale').fill('Smoke test: inspect preview only')
  await page.locator('#merge-concepts').click()
  await page.waitForFunction(() => !document.querySelector('#merge-concepts').disabled)
  if (previewRequests !== 1 || mutationRequests !== 0 || !previewDialog.includes('精确重复 1 块') || !previewDialog.includes('尚无共同名称依据')) throw new Error('Merge preview/cancel failed')
  createFixture.mergePreview = { previewRequests, mutationRequests, previewDialog }
  await registryRows.nth(0).getByRole('button', { name: '加入目录' }).click()
  createFixture.draftConcepts = await page.locator('#synthesis-draft-tree .synthesis-draft-row.concept').count()
  await page.locator('#cancel-synthesis-editor').click()

  await third.click()
  await page.locator('#synthesis-reader-view:not([hidden])').waitFor()
  await page.locator('#edit-synthesis').click()
  await page.locator('#synthesis-editor[open]').waitFor()
  const editFixture = {
    title: await page.locator('#synthesis-title-input').inputValue(),
    stableKey: await page.locator('#synthesis-key-input').inputValue(),
    stableKeyLocked: await page.locator('#synthesis-key-input').isDisabled(),
    domainLocked: await page.locator('#synthesis-domain-input').isDisabled(),
    groups: await page.locator('#synthesis-draft-tree .synthesis-draft-row.group').count(),
    concepts: await page.locator('#synthesis-draft-tree .synthesis-draft-row.concept').count(),
  }
  await page.screenshot({ path: join(outputDir, 'synthesis-editor-smoke.png'), fullPage: true })
  await page.locator('#cancel-synthesis-editor').click()

  if (createFixture.registryRows !== 80 || createFixture.serverSearchRows < 2 || createFixture.serverSearchRows >= 80 || !createFixture.mergeSourceSelected || !createFixture.mergeTargetSelected || createFixture.defaultGroups !== 1 || createFixture.draftConcepts !== 1) {
    throw new Error(`Create editor fixture failed: ${JSON.stringify(createFixture)}`)
  }
  if (editFixture.title !== '大语言模型知识体系' || editFixture.stableKey !== 'large-language-models' || !editFixture.stableKeyLocked || !editFixture.domainLocked || editFixture.groups !== 3 || editFixture.concepts !== 10) {
    throw new Error(`Edit editor fixture failed: ${JSON.stringify(editFixture)}`)
  }
  if (consoleErrors.length) throw new Error(`Browser console errors: ${consoleErrors.join('\n')}`)
  console.log(JSON.stringify({ createFixture, editFixture, consoleErrors, screenshot: join(outputDir, 'synthesis-editor-smoke.png') }, null, 2))
} finally {
  await browser.close()
}
