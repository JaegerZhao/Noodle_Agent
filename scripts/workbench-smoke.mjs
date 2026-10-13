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
  try {
    await access(candidate)
    executablePath = candidate
    break
  } catch {
    // Try the next supported Edge location.
  }
}
if (!executablePath) throw new Error('Microsoft Edge was not found; set EDGE_EXECUTABLE')

const root = resolve(import.meta.dirname, '..')
const outputDir = join(root, '.runtime')
await mkdir(outputDir, { recursive: true })
const consoleErrors = []
const browser = await chromium.launch({ executablePath, headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  let timelineRequests = 0
  let pdfRequests = 0
  page.on('request', request => {
    const path = new URL(request.url()).pathname
    if (/\/internal\/v1\/jobs\/[^/]+\/timeline$/.test(path)) timelineRequests += 1
    if (/\/internal\/v1\/sources\/[^/]+\/original-content$/.test(path)) pdfRequests += 1
  })
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('pageerror', error => consoleErrors.push(`pageerror: ${error.message}`))
  page.on('requestfailed', request => consoleErrors.push(`requestfailed: ${request.url()} (${request.failure()?.errorText || 'unknown'})`))
  await page.goto('http://127.0.0.1:8000/app/', { waitUntil: 'domcontentloaded' })
  await page.getByText('Noodle Agent', { exact: true }).first().waitFor()
  await page.locator('#source-rows tr').first().waitFor({ timeout: 10_000 }).catch(() => {})

  await page.locator('#agent-status.online, #agent-status.running').waitFor({ timeout: 15_000 })
  await page.waitForFunction(() => document.querySelectorAll('#agent-session-select option').length > 0, undefined, { timeout: 15_000 })
  if (await page.locator('#harness-frame').count()) throw new Error('Raw Harness iframe is still mounted')

  const markdownFixture = await page.evaluate(() => {
    const host = document.createElement('div')
    host.id = 'renderer-fixture'
    host.className = 'agent-message-body'
    host.style.cssText = 'position:absolute;left:-9999px;width:260px'
    document.body.append(host)
    const sample = String.raw`**重点**与行内公式 $x_i^2$。

$$
y = \sum_{i=1}^{n} x_i + \frac{veryLongNumerator_{abcdefghijk}}{veryLongDenominator_{lmnopqrstuv}}
$$

| A | B | SourceNote ID |
|---|---|---|
| 1 | 2 | \`71d0a51b-2428-4e80-8c3f-7ce3e04dcaaa\` |` + '\n\n`code_x`\n\n```txt\nvery_long_code_line_without_breaks_123456789abcdefghijklmnopqrstuvwxyz\n```'
    renderMarkdown(host, sample)
    const conceptMath = renderConceptContentBlock({
      block_key: 'formula-symbol-fixture',
      type: 'FORMULA',
      latex: String.raw`y = x^2`,
      symbols: [
        { symbol: String.raw`$x$`, meaning: String.raw`input $x$` },
        { symbol: 'A_{i,j}', description: '注意力矩阵元素' },
        { symbol: 'q_i', meaning: '查询向量' },
        { symbol: String.raw`\eta(x)`, description: '随机特征映射' },
        { symbol: String.raw`\(k_j\)`, meaning: '键向量' },
        { symbol: String.raw`\[D\]`, meaning: '特征维度' },
        { symbol: String.raw`$$\theta^T$$`, meaning: '转置' },
      ],
    }, new Map(), new Set(), { blockTitle: 'Formula', conceptTitle: 'Fixture' })
    const assetMath = document.createElement('div')
    appendConceptAsset(assetMath, {
      asset_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
      caption: String.raw`kernel $k \times k$`,
      page_number: 1,
    }, 'Figure', 'Fixture')
    host.append(conceptMath, assetMath)
    const tableRegion = host.querySelector('.markdown-table-region')
    const formulaRegion = host.querySelector('.noodle-math-display.markdown-scroll-region')
    const codeRegion = host.querySelector('pre.markdown-scroll-region')
    const result = {
      strong: host.querySelectorAll('strong').length,
      katex: host.querySelectorAll('.katex').length,
      tables: host.querySelectorAll('table').length,
      code: host.querySelectorAll('code').length,
      rawMath: host.innerText.includes('$$'),
      warnings: host.querySelectorAll('.math-render-warning').length,
      tableWrapped: Boolean(tableRegion),
      tableScrollable: Boolean(tableRegion && tableRegion.scrollWidth > tableRegion.clientWidth),
      formulaScrollable: Boolean(formulaRegion && getComputedStyle(formulaRegion).overflowX === 'auto'),
      codeScrollable: Boolean(codeRegion && getComputedStyle(codeRegion).overflowX === 'auto'),
      parentContained: host.scrollWidth <= host.clientWidth + 1,
      conceptSymbolKatex: conceptMath.querySelectorAll('.concept-symbols .katex').length,
      conceptSymbolTerms: conceptMath.querySelectorAll('.concept-symbols dt').length,
      conceptRenderedTerms: conceptMath.querySelectorAll('.concept-symbols dt .katex').length,
      conceptSymbolSubscripts: conceptMath.querySelectorAll('.concept-symbols dt .msupsub').length,
      conceptLegacyDefinition: conceptMath.querySelectorAll('.concept-symbols dd')[1].textContent,
      assetCaptionKatex: assetMath.querySelectorAll('figcaption .katex').length,
      conceptRawDollar: conceptMath.innerText.includes('$') || assetMath.innerText.includes('$'),
    }
    host.remove()
    return result
  })
  if (!markdownFixture.strong || markdownFixture.katex < 2 || !markdownFixture.tables || !markdownFixture.code || markdownFixture.rawMath || markdownFixture.warnings || !markdownFixture.tableWrapped || !markdownFixture.tableScrollable || !markdownFixture.formulaScrollable || !markdownFixture.codeScrollable || !markdownFixture.parentContained || markdownFixture.conceptSymbolKatex < 8 || markdownFixture.conceptRenderedTerms !== markdownFixture.conceptSymbolTerms || markdownFixture.conceptSymbolSubscripts < 3 || markdownFixture.conceptLegacyDefinition !== '注意力矩阵元素' || !markdownFixture.assetCaptionKatex || markdownFixture.conceptRawDollar) {
    throw new Error(`Markdown/KaTeX fixture failed: ${JSON.stringify(markdownFixture)}`)
  }

  const legacyReaderFixture = await page.evaluate(() => {
    const host = document.createElement('div')
    host.style.cssText = 'position:absolute;left:-9999px;width:600px'
    document.body.append(host)
    const context = { noteId: 'fixture', outlineNodeId: 'fixture', renderedCaptions: new Set() }
    const rendered = new Set()
    const blocks = [
      { type: 'FORMULA', text: String.raw`说明：$$x^2$$` },
      { type: 'TABLE', text: '| A | B |\n|---|---|\n| 1 | 2 |', table_headers: [] },
      { type: 'NUMBERED_LIST', text: '1. One\n2. Two', items: [] },
      ...[1, 2].map(id => ({ type: 'IMAGE', text: '$x^2$ 同一图注', assets: [{ visual_asset_id: String(id), storage_key: `${id}.png`, blob_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=', page_index: 1, page_label: '2', caption: '$x^2$ 同一图注' }] })),
    ]
    for (const block of blocks) host.append(renderBlock(block, rendered, context))
    const result = { warnings: host.querySelectorAll('.math-render-warning').length, tables: host.querySelectorAll('table').length, listRows: host.querySelectorAll('li').length, images: host.querySelectorAll('img').length, captions: host.querySelectorAll('.asset-caption-text').length, captionMath: host.querySelectorAll('.asset-caption-text .katex').length }
    host.remove()
    return result
  })
  if (legacyReaderFixture.warnings || legacyReaderFixture.tables !== 1 || legacyReaderFixture.listRows !== 2 || legacyReaderFixture.images !== 2 || legacyReaderFixture.captions !== 1 || legacyReaderFixture.captionMath !== 1) throw new Error(`Legacy reading fixture failed: ${JSON.stringify(legacyReaderFixture)}`)

  const historyFilterFixture = await page.evaluate(() => {
    const message = (kind, text, seq) => ({
      event: {
        type: 'user/message',
        seq,
        time: 1_700_000_000_000 + seq,
        data: { role: 'user', source: { kind }, content: [{ type: 'text', text }] },
      },
    })
    const folded = foldAgentHistory([
      message('user', '真正的用户问题', 1),
      message('plugin', 'Current runtime context', 2),
      message('skill-catalog', '<available_skills>internal</available_skills>', 3),
      message('future-internal-source', 'unknown internal context', 4),
    ])
    return folded.messages.map(item => ({ role: item.role, text: item.text }))
  })
  if (historyFilterFixture.length !== 1 || historyFilterFixture[0].text !== '真正的用户问题') {
    throw new Error(`Harness history source filter failed: ${JSON.stringify(historyFilterFixture)}`)
  }

  const clipboardAttachmentFixture = await page.evaluate(() => {
    const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII='), char => char.charCodeAt(0))
    const file = new File([bytes], 'image.png', { type: 'image/png' })
    const transfer = new DataTransfer()
    transfer.items.add(file)
    let prevented = false
    handleAgentPaste({ clipboardData: transfer, preventDefault: () => { prevented = true } })
    const card = document.querySelector('#agent-attachment-list .agent-attachment-card')
    const result = {
      prevented,
      shelfVisible: !document.querySelector('#agent-attachment-shelf').hidden,
      cards: document.querySelectorAll('#agent-attachment-list .agent-attachment-card').length,
      preview: card?.querySelector('img')?.src.startsWith('blob:') || false,
      removable: Boolean(card?.querySelector('.agent-attachment-remove')),
    }
    clearAgentAttachments()
    return result
  })
  if (!clipboardAttachmentFixture.prevented || !clipboardAttachmentFixture.shelfVisible || clipboardAttachmentFixture.cards !== 1 || !clipboardAttachmentFixture.preview || !clipboardAttachmentFixture.removable) {
    throw new Error(`Clipboard attachment fixture failed: ${JSON.stringify(clipboardAttachmentFixture)}`)
  }

  const compactContextFixture = await page.evaluate(() => {
    const previous = state.sourceNote
    state.sourceNote = {
      source_note_id: 'source-note-1',
      version_id: 'source-note-version-2',
      title: '卷积神经网络',
      source_file: 'large-private-path-that-should-not-be-sent.pdf',
    }
    const selection = agentPrompt({ note_id: 'note-1', version_id: 'note-version-3', title: '卷积核' })
    state.sourceNote = previous
    const attachment = attachmentAgentContext([{
      name: 'diagram.png',
      remote: {
        attachment_id: 'attachment-1', original_name: 'diagram.png', media_type: 'image/png', status: 'READY',
        vision: {
          asset_type: 'DIAGRAM', caption: '卷积过程示意图', teaching_role: '解释局部感受野', essential: true,
          image_sha256: 'must-not-leak', model: 'must-not-leak', prompt_version: 'must-not-leak',
          factual_observations: ['输入与卷积核逐位置相乘后求和'],
        },
      },
    }])
    return {
      selectionChars: selection.length,
      attachmentChars: attachment.length,
      hasIds: selection.includes('source_note_version_id=source-note-version-2') && selection.includes('atomic_note_version_id=note-version-3'),
      leaksPath: selection.includes('large-private-path'),
      leaksVisionMetadata: attachment.includes('must-not-leak'),
      hasVisionSummary: attachment.includes('卷积过程示意图') && attachment.includes('解释局部感受野'),
    }
  })
  if (!compactContextFixture.hasIds || compactContextFixture.leaksPath || compactContextFixture.leaksVisionMetadata || !compactContextFixture.hasVisionSummary || compactContextFixture.selectionChars > 500 || compactContextFixture.attachmentChars > 1800) {
    throw new Error(`Compact context fixture failed: ${JSON.stringify(compactContextFixture)}`)
  }

  const executionFixture = await page.evaluate(() => {
    const event = (type, seq, data = {}) => ({ event: { type, seq, time: 1_700_000_000_000 + seq * 100, data } })
    const entries = [
      event('turn/start', 1, { turn: 1 }),
      event('user/message', 2, { source: { kind: 'user' }, content: [{ type: 'text', text: '查找证据并修改笔记' }] }),
      event('assistant/message', 3, { turn: 1, message: { content: [{ type: 'reasoning', text: 'RAW_PRIVATE_REASONING' }] } }),
      event('tool/call', 4, { turn: 1, step: 1, callId: 'call-search', name: 'noodle_search_knowledge', arguments: '{"query":"CNN","limit":3}' }),
      event('tool/result', 5, { turn: 1, step: 1, message: { source: { kind: 'tool', callId: 'call-search' }, content: [{ type: 'tool-result', toolCallId: 'call-search', content: [{ type: 'text', text: '{"count":2,"hits":[]}' }], isError: false }] } }),
      event('tool/call', 6, { turn: 1, step: 2, callId: 'call-edit', name: 'noodle_edit_note_block', arguments: '{"note_id":"note-1","block_key":"b-1"}' }),
      event('approval/asked', 7, { id: 'approval-1', callId: 'call-edit', toolName: 'noodle_edit_note_block', reason: '将创建新的不可变笔记版本' }),
      event('assistant/message', 8, { turn: 1, message: { content: [{ type: 'reasoning', text: 'RAW_PRIVATE_REASONING_2' }, { type: 'text', text: '已找到相关证据，修改正在等待审批。' }] } }),
    ]
    const folded = foldAgentHistory(entries)
    const host = document.createElement('div')
    host.style.cssText = 'position:absolute;left:-9999px;width:340px'
    const trail = renderAgentExecution(folded.turns[0])
    host.append(trail)
    document.body.append(host)
    const result = {
      turns: folded.turns.length,
      tools: trail.querySelectorAll('.agent-tool-step').length,
      completed: trail.querySelectorAll('.agent-tool-step.completed').length,
      approvals: trail.querySelectorAll('.agent-tool-step.approval').length,
      approvalLinks: trail.querySelectorAll('.agent-approval-callout a').length,
      approvalButtons: trail.querySelectorAll('.agent-approval-actions button').length,
      allowLabel: trail.querySelector('.agent-approval-allow')?.textContent,
      rejectLabel: trail.querySelector('.agent-approval-reject')?.textContent,
      argumentDetails: trail.querySelectorAll('.agent-tool-details').length,
      visibleReasoning: host.textContent.includes('RAW_PRIVATE_REASONING') || folded.messages.some(message => message.text?.includes('RAW_PRIVATE_REASONING')),
      summary: trail.querySelector('.agent-execution-summary')?.textContent || '',
    }
    host.remove()
    return result
  })
  if (executionFixture.turns !== 1 || executionFixture.tools !== 2 || executionFixture.completed !== 1 || executionFixture.approvals !== 1 || executionFixture.approvalLinks !== 1 || executionFixture.approvalButtons !== 2 || executionFixture.allowLabel !== '仅允许本次' || executionFixture.rejectLabel !== '拒绝' || executionFixture.argumentDetails !== 2 || executionFixture.visibleReasoning) {
    throw new Error(`Agent execution fixture failed: ${JSON.stringify(executionFixture)}`)
  }

  const toolSummaryFixture = await page.evaluate(() => {
    const search = payload => toolResultSummary({ name: 'noodle_search_knowledge', result: { payload, isError: false } });
    const spilled = toolResultData({ data: { message: { source: { callId: 'spill' }, content: [{ type: 'tool-result', content: [{ type: 'text', text: '{"count":14,"hits":[...\n(Omitted 80543 bytes. Full formatted result stored at: example.txt)' }] }] } } });
    return {
      empty: search({ count: 0, hits: [] }),
      complete: search({ count: 14, hits: [] }),
      spilled: search(spilled.payload),
      citations: toolResultSummary({ name: 'noodle_answer_knowledge', result: { payload: { citations: [{}, {}] }, isError: false } }),
    };
  });
  if (toolSummaryFixture.empty !== '命中 0 条知识结果' || toolSummaryFixture.complete !== '命中 14 条知识结果' || toolSummaryFixture.spilled !== '检索已完成，长结果摘要不可用' || toolSummaryFixture.citations !== '已生成回答 · 2 条笔记引用') {
    throw new Error(`Tool summary metadata regression: ${JSON.stringify(toolSummaryFixture)}`);
  }

  const warningFixture = await page.evaluate(() => {
    const panel = renderJobWarnings({
      warnings: [
        {
          code: 'SKIPPED_SECTION',
          severity: 'INFO',
          section: '结语',
          detail: '该窗口仅包含结语页的标题，无实质可复用的知识内容。',
        },
      ],
    })
    document.body.append(panel)
    const result = {
      heading: panel.querySelector('.job-warnings-heading strong')?.textContent,
      count: panel.querySelector('.job-warning-count')?.textContent,
      code: panel.querySelector('.job-warning-code')?.textContent,
      section: panel.querySelector('.job-warning-section')?.textContent,
      detail: panel.querySelector('.job-warning-detail')?.textContent,
      infoOnly: panel.classList.contains('info-only'),
      severity: panel.querySelector('.job-warning-severity')?.textContent,
      scrollable: getComputedStyle(panel.querySelector('.job-warning-list')).overflowY === 'auto',
    }
    panel.remove()
    return result
  })
  if (warningFixture.heading !== '处理说明' || warningFixture.count !== '1' || warningFixture.code !== 'SKIPPED_SECTION' || warningFixture.section !== '章节：结语' || !warningFixture.detail || !warningFixture.scrollable || !warningFixture.infoOnly || warningFixture.severity !== '提示') {
    throw new Error(`Job warning fixture failed: ${JSON.stringify(warningFixture)}`)
  }

  const sourceRefreshFixture = await page.evaluate(() => ({
    changed: sourceReleaseChanged(
      { version_id: 'source-note-version-1' },
      { source_note_version_id: 'source-note-version-2' },
    ),
    unchanged: sourceReleaseChanged(
      { version_id: 'source-note-version-2' },
      { source_note_version_id: 'source-note-version-2' },
    ),
  }))
  if (!sourceRefreshFixture.changed || sourceRefreshFixture.unchanged) {
    throw new Error(`SourceNote refresh detection failed: ${JSON.stringify(sourceRefreshFixture)}`)
  }

  const sourceActionFixture = await page.evaluate(() => {
    const failed = renderSourceActions({
      source_id: 'source-failed',
      title: '失败来源',
      job: { id: 'job-failed', status: 'FAILED' },
      source_note_id: null,
    })
    const running = renderSourceActions({
      source_id: 'source-running',
      title: '运行中来源',
      job: { id: 'job-running', status: 'RUNNING' },
      source_note_id: null,
    })
    return {
      failed: [...failed.querySelectorAll('button')].map(button => button.textContent),
      empty: [...renderSourceActions({ source_id: 'source-empty', title: '空来源', job: null, source_note_id: null }).querySelectorAll('button')].map(button => button.textContent),
      runningDeleteDisabled: running.querySelector('.source-action.danger')?.disabled,
    }
  })
  if (sourceActionFixture.failed.join('|') !== '断点重试|删除来源' || sourceActionFixture.empty.join('|') !== '删除来源' || !sourceActionFixture.runningDeleteDisabled) {
    throw new Error(`Source action fixture failed: ${JSON.stringify(sourceActionFixture)}`)
  }

  let chatTableReady = await page.locator('#agent-messages table').first().waitFor({ timeout: 2_500 }).then(() => true).catch(() => false)
  if (!chatTableReady) {
    const sessionIds = await page.locator('#agent-session-select option').evaluateAll(options => options.map(option => option.value))
    for (const sessionId of sessionIds) {
      await page.locator('#agent-session-select').selectOption(sessionId)
      chatTableReady = await page.locator('#agent-messages table').first().waitFor({ timeout: 2_500 }).then(() => true).catch(() => false)
      if (chatTableReady) break
    }
  }
  if (!chatTableReady) throw new Error('No Harness session with a rendered Markdown table is available for overflow verification')
  const chatOverflow = await page.evaluate(() => {
    const panel = document.querySelector('#agent-messages')
    const tables = [...panel.querySelectorAll('.agent-message-body table')]
    const escaped = [...panel.querySelectorAll('.agent-message-body *')].filter(node => {
      // KaTeX's internal spans expose intrinsic glyph widths even when their
      // owning math region is correctly contained. Test user-visible regions,
      // not those implementation nodes, to avoid sub-pixel false positives.
      if (node.closest('.markdown-scroll-region, .katex') || ['auto', 'scroll'].includes(getComputedStyle(node).overflowX)) return false
      return node.scrollWidth > node.clientWidth + 1
    })
    return {
      panelContained: panel.scrollWidth <= panel.clientWidth + 1,
      tables: tables.length,
      wrappedTables: tables.filter(table => table.parentElement?.classList.contains('markdown-table-region')).length,
      escaped: escaped.map(node => ({ tag: node.tagName, className: node.className, text: node.textContent?.slice(0, 80), width: node.clientWidth, scrollWidth: node.scrollWidth })).slice(0, 10),
    }
  })
  if (!chatOverflow.panelContained || !chatOverflow.tables || chatOverflow.wrappedTables !== chatOverflow.tables || chatOverflow.escaped.length) {
    throw new Error(`Chat overflow containment failed: ${JSON.stringify(chatOverflow)}`)
  }
  const realExecution = await page.evaluate(() => ({
    trails: document.querySelectorAll('#agent-messages .agent-execution').length,
    toolSteps: document.querySelectorAll('#agent-messages .agent-tool-step').length,
    rawReasoningPanels: document.querySelectorAll('#agent-messages .agent-reasoning').length,
    completedSteps: document.querySelectorAll('#agent-messages .agent-tool-step.completed').length,
    failedSteps: document.querySelectorAll('#agent-messages .agent-tool-step.failed').length,
    rejectedSteps: document.querySelectorAll('#agent-messages .agent-tool-step.rejected').length,
    approvalSteps: document.querySelectorAll('#agent-messages .agent-tool-step.approval').length,
    runningSteps: document.querySelectorAll('#agent-messages .agent-tool-step.running').length,
  }))
  const classifiedSteps = realExecution.completedSteps + realExecution.failedSteps + realExecution.rejectedSteps + realExecution.approvalSteps + realExecution.runningSteps
  if (!realExecution.trails || !realExecution.toolSteps || realExecution.rawReasoningPanels || classifiedSteps !== realExecution.toolSteps || !realExecution.completedSteps) {
    throw new Error(`Real Agent execution rendering failed: ${JSON.stringify(realExecution)}`)
  }
  const agentTrailScreenshot = join(outputDir, 'agent-execution-trail.png')
  await page.locator('#agent-messages .agent-execution').first().screenshot({ path: agentTrailScreenshot })
  const chatTableScreenshot = join(outputDir, 'chat-table-scroll.png')
  await page.locator('#agent-messages .markdown-table-region').first().screenshot({ path: chatTableScreenshot })

  let readerMath = null
  let readerToolbar = null
  let readerSync = null
  let readerPdf = null
  let readerPdfScreenshot = null
  const cnnCompleted = page.locator('#source-rows tr').filter({ hasText: 'CNN precision verification' }).filter({ has: page.locator('.status-chip.completed') }).first()
  const completed = await cnnCompleted.count()
    ? cnnCompleted
    : page.locator('#source-rows tr').filter({ has: page.locator('.status-chip.completed') }).first()
  if (await completed.count()) {
    await completed.locator('.source-name-button').click()
    await page.locator('#reader-view:not([hidden])').waitFor()
    await page.locator('#source-note-body .atomic-note').first().waitFor({ timeout: 10_000 })
    readerToolbar = await page.evaluate(async () => {
      const reader = document.querySelector('#reader-view')
      const toolbar = document.querySelector('#reader-view .reader-topbar')
      reader.scrollTop = Math.min(500, reader.scrollHeight - reader.clientHeight)
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      const result = {
        position: getComputedStyle(toolbar).position,
        pinnedOffset: Math.round(toolbar.getBoundingClientRect().top - reader.getBoundingClientRect().top),
        backButtons: toolbar.querySelectorAll('#back-to-library').length,
        modeButtons: toolbar.querySelectorAll('[data-content-mode]').length,
        title: document.querySelector('#reader-toolbar-title')?.textContent?.trim() || '',
        copyButtons: document.querySelectorAll('#copy-note-id').length,
      }
      reader.scrollTop = 0
      return result
    })
    if (readerToolbar.position !== 'sticky' || Math.abs(readerToolbar.pinnedOffset) > 1 || readerToolbar.backButtons !== 1 || readerToolbar.modeButtons !== 2 || !readerToolbar.title || readerToolbar.copyButtons) {
      throw new Error(`Reader toolbar integration failed: ${JSON.stringify(readerToolbar)}`)
    }
    readerMath = await page.evaluate(() => ({
      katex: document.querySelectorAll('#source-note-body .katex').length,
      warnings: document.querySelectorAll('#source-note-body .math-render-warning').length,
      tables: document.querySelectorAll('#source-note-body table').length,
    }))
    const articleAskButtons = await page.locator('#reader-view button').filter({ hasText: /询问\s*Agent/ }).count()
    const noteToPdf = await page.evaluate(async () => {
      const anchors = state.navigationIndex?.anchors || []
      const candidate = anchors.find(anchor => anchor.block_key && physicalPage(anchor) > 1 && noteElementForAnchor(anchor))
        || anchors.find(anchor => anchor.block_key && noteElementForAnchor(anchor))
        || anchors[0]
      if (!candidate) return null
      const target = noteElementForAnchor(candidate)
      target?.scrollIntoView({ behavior: 'auto', block: 'start' })
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      const detected = syncNoteAnchorFromScroll({ force: true }) || candidate
      return {
        anchorCount: anchors.length,
        blockAnchors: anchors.filter(anchor => anchor.block_key).length,
        expectedPage: physicalPage(detected),
        expectedNoteId: detected.note_id,
        expectedBlockKey: detected.block_key,
      }
    })
    if (!noteToPdf?.anchorCount || !noteToPdf.blockAnchors || !noteToPdf.expectedPage) {
      throw new Error(`Reader navigation anchors missing: ${JSON.stringify(noteToPdf)}`)
    }
    await page.locator('[data-content-mode="pdf"]').click()
    await page.locator('#pdf-content-pane:not([hidden])').waitFor()
    try {
      await page.locator('#source-pdf-pages .source-pdf-page[data-rendered="true"] canvas').first().waitFor({ timeout: 20_000 })
    } catch (error) {
      const pdfDiagnostics = await page.evaluate(() => ({
        status: document.querySelector('#pdf-load-status')?.textContent,
        pages: document.querySelectorAll('#source-pdf-pages .source-pdf-page').length,
        rendered: document.querySelectorAll('#source-pdf-pages .source-pdf-page[data-rendered="true"]').length,
        errors: [...document.querySelectorAll('.source-pdf-page-error')].map(node => node.textContent),
        toasts: [...document.querySelectorAll('.toast')].map(node => node.textContent),
        contentMode: state.contentMode,
        selectedSourceId: state.selectedSource?.source_id,
        documentPages: state.pdfDocument?.numPages || 0,
        hasPagesPromise: Boolean(state.pdfPagesPromise),
        hasPdfJsPromise: Boolean(state.pdfJsPromise),
        hasLoadingTask: Boolean(state.pdfLoadingTask),
        pdfDataLength: state.pdfData?.length || 0,
      }))
      throw new Error(`PDF.js did not render: ${JSON.stringify({ pdfDiagnostics, consoleErrors, cause: error.message })}`)
    }
    const pdfLayout = await page.evaluate(() => {
      const viewer = document.querySelector('#source-pdf-viewer')
      const page = document.querySelector('#source-pdf-pages .source-pdf-page[data-rendered="true"]')
      const canvas = page?.querySelector('canvas')
      if (!viewer || !page || !canvas) return null
      return {
        viewerWidth: viewer.clientWidth,
        pageWidth: page.getBoundingClientRect().width,
        canvasWidth: canvas.getBoundingClientRect().width,
        viewerBackground: getComputedStyle(viewer).backgroundColor,
        viewerOverflow: getComputedStyle(viewer).overflowY,
        renderedPages: document.querySelectorAll('#source-pdf-pages .source-pdf-page[data-rendered="true"]').length,
      }
    })
    readerPdf = {
      sourceId: await page.evaluate(() => state.selectedSource?.source_id || ''),
      articleAskButtons,
      contentModes: await page.locator('[data-content-mode]').count(),
      evidenceButtons: await page.locator('#source-note-body .evidence-chip').count(),
      pdfRequests,
      pdfLayout,
      openHref: await page.locator('#open-source-pdf').getAttribute('href'),
      loadStatus: await page.locator('#pdf-load-status').textContent(),
      pageLabel: await page.locator('#pdf-page-label').textContent(),
      pdfVisible: await page.locator('#pdf-content-pane:not([hidden])').count() === 1,
    }
    const fillsViewer = pdfLayout && Math.abs(pdfLayout.viewerWidth - pdfLayout.pageWidth) <= 12 && Math.abs(pdfLayout.pageWidth - pdfLayout.canvasWidth) <= 2
    if (articleAskButtons || readerPdf.contentModes !== 2 || pdfRequests < 1 || !readerPdf.openHref?.startsWith('blob:') || !readerPdf.pdfVisible || !fillsViewer || pdfLayout.renderedPages < 1 || readerPdf.pageLabel !== `P.${noteToPdf.expectedPage}`) {
      throw new Error(`Reader/PDF integration failed: ${JSON.stringify(readerPdf)}`)
    }
    readerPdfScreenshot = join(outputDir, 'reader-pdf-smoke.png')
    await page.locator('#pdf-content-pane').screenshot({ path: readerPdfScreenshot })
    const directoryJump = await page.evaluate(() => {
      const buttons = [...document.querySelectorAll('#outline-tree .outline-link')]
      for (const button of buttons) {
        const candidates = button.dataset.noteId
          ? state.navigationIndex?.byNote.get(button.dataset.noteId)
          : state.navigationIndex?.byOutline.get(button.dataset.outlineNodeId)
        const anchor = chooseNavigationAnchor(candidates)
        const pageNumber = physicalPage(anchor)
        if (anchor && pageNumber !== state.pdfPage && pageNumber < (state.pdfDocument?.numPages || Infinity)) {
          button.click()
          return { pageNumber, noteId: anchor.note_id, outlineNodeId: anchor.outline_node_id }
        }
      }
      return null
    })
    if (directoryJump) await page.waitForFunction(target => state.pdfPage === target.pageNumber, directoryJump, { timeout: 5_000 })
    await page.waitForTimeout(850)
    const pdfToNoteTarget = await page.evaluate(() => {
      const candidate = [...(state.navigationIndex?.anchors || [])]
        .reverse()
        .find(anchor => anchor.block_key
          && physicalPage(anchor) !== state.pdfPage
          && physicalPage(anchor) < (state.pdfDocument?.numPages || Infinity) - 1
          && state.pdfPageElements.has(physicalPage(anchor)))
      if (!candidate) return null
      const pageNumber = physicalPage(candidate)
      const shell = state.pdfPageElements.get(pageNumber)
      const viewer = document.querySelector('#source-pdf-viewer')
      const pageTop = viewer.scrollTop + shell.getBoundingClientRect().top - viewer.getBoundingClientRect().top
      viewer.scrollTo({ top: pageTop, behavior: 'auto' })
      return { pageNumber, noteId: candidate.note_id, blockKey: candidate.block_key, outlineNodeId: candidate.outline_node_id }
    })
    if (pdfToNoteTarget) {
      try {
        await page.waitForFunction(target => state.pdfPage === target.pageNumber, pdfToNoteTarget, { timeout: 5_000 })
      } catch (error) {
        const location = await page.evaluate(() => ({ page: state.pdfPage, scrollTop: document.querySelector('#source-pdf-viewer')?.scrollTop, anchor: state.readingAnchor }))
        throw new Error(`PDF scroll tracking failed: ${JSON.stringify({ target: pdfToNoteTarget, location, cause: error.message })}`)
      }
      Object.assign(pdfToNoteTarget, await page.evaluate(() => ({
        resolvedNoteId: state.readingAnchor?.note_id || null,
        resolvedBlockKey: state.readingAnchor?.block_key || null,
      })))
    }
    const pdfDirectory = await page.evaluate(() => ({
      activeNotes: document.querySelectorAll('#outline-tree .navigation-active').length,
      activeParents: document.querySelectorAll('#outline-tree .navigation-parent').length,
    }))
    await page.locator('[data-content-mode="note"]').click()
    await page.locator('#note-content-pane:not([hidden])').waitFor()
    await page.waitForTimeout(900)
    const pdfToNote = await page.evaluate(() => {
      const anchor = state.readingAnchor
      const target = noteElementForAnchor(anchor)
      const reader = document.querySelector('#reader-view')
      const toolbar = document.querySelector('.reader-topbar')
      const bounds = target?.getBoundingClientRect()
      const expectedTop = reader.getBoundingClientRect().top + toolbar.getBoundingClientRect().height
      return {
        noteId: anchor?.note_id || null,
        blockKey: anchor?.block_key || null,
        targetFound: Boolean(target),
        targetVisible: Boolean(bounds && bounds.bottom > expectedTop && bounds.top < reader.getBoundingClientRect().bottom),
        targetNearTop: Boolean(bounds && Math.abs(bounds.top - expectedTop) < 180),
        targetTop: bounds?.top ?? null,
        targetBottom: bounds?.bottom ?? null,
        readerTop: reader.getBoundingClientRect().top,
        readerBottom: reader.getBoundingClientRect().bottom,
        readerScrollTop: reader.scrollTop,
        readerScrollHeight: reader.scrollHeight,
        activeNotes: document.querySelectorAll('#outline-tree .navigation-active').length,
      }
    })
    readerSync = { noteToPdf, directoryJump, pdfToNoteTarget, pdfDirectory, pdfToNote }
    const reverseIdentityMatches = !pdfToNoteTarget || (pdfToNote.noteId === pdfToNoteTarget.resolvedNoteId && pdfToNote.blockKey === pdfToNoteTarget.resolvedBlockKey)
    if (!pdfDirectory.activeNotes || !pdfDirectory.activeParents || !pdfToNote.targetFound || !pdfToNote.targetVisible || !pdfToNote.activeNotes || !reverseIdentityMatches) {
      throw new Error(`Bidirectional reader sync failed: ${JSON.stringify(readerSync)}`)
    }
  }

  await page.locator('#job-drawer-toggle').click()
  await page.locator('#job-drawer:not(.collapsed)').waitFor()
  const timelineBaseline = timelineRequests
  const firstJob = page.locator('#job-list .job-row').first()
  if (await firstJob.count()) {
    await firstJob.click()
    await page.waitForTimeout(3200)
  }
  const jobRefresh = {
    timelineRequests: timelineRequests - timelineBaseline,
    liveClocks: await page.locator('#job-drawer .live-duration').count(),
    sourceClocks: await page.locator('#source-rows .live-duration').count(),
    detailClock: await page.locator('#job-detail .job-detail-clock').count(),
    detailClockTag: await page.locator('#job-detail .job-detail-clock').evaluate((node) => node.tagName),
    visibleDurationLabels: await page.locator('#job-detail .job-metric > span').evaluateAll((nodes) => (
      nodes.map((node) => node.textContent?.trim()).filter((text) => ['总用时', '已运行'].includes(text)).length
    )),
    modelDurationLabels: await page.locator('#job-detail .job-metric > span').evaluateAll((nodes) => (
      nodes.map((node) => node.textContent?.trim()).filter((text) => ['模型墙钟', '模型累计', '模型耗时'].includes(text)).length
    )),
    elapsedMath: await page.evaluate(() => ({
      running: jobElapsedMs({ status: 'RUNNING', created_at: new Date(1000).toISOString() }, 6000),
      terminal: jobElapsedMs({ status: 'COMPLETED', created_at: new Date(1000).toISOString(), updated_at: new Date(9000).toISOString() }, 99_000),
      legacyNaiveUtc: jobElapsedMs({ status: 'RUNNING', created_at: '1970-01-01T00:00:01' }, 6000),
      oneSecondTick: jobElapsedMs({ status: 'RUNNING', created_at: new Date(1000).toISOString() }, 6000)
        - jobElapsedMs({ status: 'RUNNING', created_at: new Date(1000).toISOString() }, 5000),
      minuteBoundary: formatDuration(60_000),
      hourBoundary: formatDuration(3_601_000),
      shanghaiTime: formatDate('2026-08-28T03:02:49Z'),
    })),
  }
  const clockMath = jobRefresh.elapsedMath
  if (await firstJob.count() && (
    jobRefresh.timelineRequests < 2
    || !jobRefresh.liveClocks
    || !jobRefresh.sourceClocks
    || !jobRefresh.detailClock
    || jobRefresh.detailClockTag !== 'B'
    || jobRefresh.visibleDurationLabels !== 1
    || jobRefresh.modelDurationLabels !== 0
    || clockMath.running !== 5000
    || clockMath.terminal !== 8000
    || clockMath.legacyNaiveUtc !== 5000
    || clockMath.oneSecondTick !== 1000
    || clockMath.minuteBoundary !== '1m 00s'
    || clockMath.hourBoundary !== '1h 0m 01s'
    || !clockMath.shanghaiTime.includes('11:02:49')
  )) {
    throw new Error(`Job auto-refresh fixture failed: ${JSON.stringify(jobRefresh)}`)
  }

  await page.screenshot({ path: join(outputDir, 'workbench-smoke.png'), fullPage: true })
  const result = {
    sourceRows: await page.locator('#source-rows tr').count(),
    readerOpened: await page.locator('#reader-view:not([hidden])').count() === 1,
    customAgentConnected: await page.locator('#agent-status.online, #agent-status.running').count() === 1,
    agentSessions: await page.locator('#agent-session-select option').count(),
    sessionRenameButtons: await page.locator('#rename-agent-session').count(),
    markdownFixture,
    legacyReaderFixture,
    historyFilterFixture,
    clipboardAttachmentFixture,
    compactContextFixture,
    executionFixture,
    toolSummaryFixture,
    warningFixture,
    sourceActionFixture,
    chatOverflow,
    realExecution,
    agentTrailScreenshot,
    chatTableScreenshot,
    readerMath,
    readerToolbar,
    readerSync,
    readerPdf,
    readerPdfScreenshot,
    jobRefresh,
    consoleErrors,
    screenshot: join(outputDir, 'workbench-smoke.png'),
  }
  if (result.sessionRenameButtons !== 1) {
    throw new Error(`Session rename control is missing: ${JSON.stringify(result)}`)
  }
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
  if (consoleErrors.length) process.exitCode = 1
} finally {
  await browser.close()
}
