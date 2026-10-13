import { access, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { chromium } from 'playwright-core'

const candidates = [process.env.EDGE_EXECUTABLE,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/chromium', '/usr/bin/google-chrome'].filter(Boolean)
let executablePath
for (const candidate of candidates) {
  try { await access(candidate); executablePath = candidate; break } catch { /* next */ }
}
if (!executablePath) throw new Error('Set EDGE_EXECUTABLE to a Chromium browser for --browser')
const browser = await chromium.launch({ executablePath, headless: true })
const errors = []
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${process.env.NOODLE_API_BASE_URL}/app/`, { waitUntil: 'domcontentloaded' })
  await page.locator('#source-rows tr').first().waitFor()
  await page.waitForFunction(() => state.sources?.some(source => source.source_note_id))
  // Real isolated API data; renderer helper is exercised with synthetic math.
  await page.evaluate(async () => {
    const source = state.sources.find(item => item.source_note_id)
    await openSource(source)
  })
  await page.locator('#reader-view:not([hidden])').waitFor()
  const renderer = await page.evaluate(() => {
    const host = document.createElement('div')
    renderMarkdown(host, '**重点** $x^2$\n\n$$\\frac{1}{2}$$\n\n| A | B |\n|---|---|\n| 1 | 2 |')
    return { math: host.querySelectorAll('.katex').length,
      tables: host.querySelectorAll('table').length,
      strong: host.querySelectorAll('strong').length,
      warnings: host.querySelectorAll('.math-render-warning').length }
  })
  if (renderer.math < 2 || renderer.tables !== 1 || !renderer.strong || renderer.warnings) {
    throw new Error(`Renderer failed: ${JSON.stringify(renderer)}`)
  }
  await page.locator('[data-content-mode="pdf"]').click()
  await page.waitForFunction(() => Boolean(state.pdfDocument?.numPages), undefined, { timeout: 20000 })
  await page.locator('#source-pdf-viewer canvas').first().waitFor()
  await page.locator('[data-content-mode="note"]').click()
  if (errors.length) throw new Error(JSON.stringify(errors))
  const report = { ok: true, renderer, pageErrors: errors, browser: await browser.version() }
  await page.screenshot({ path: join(process.env.DELIVERY_OUTPUT_DIR, 'reader.png'), fullPage: false })
  await writeFile(join(process.env.DELIVERY_OUTPUT_DIR, 'browser.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report))
} finally {
  await browser.close()
}
