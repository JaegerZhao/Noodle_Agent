import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import { NoodleApiError, NoodleHttpProvider } from '../src/http-provider.js'

let server: ReturnType<typeof createServer>
let baseUrl = ''
let lastUrl = ''
let lastBody: unknown = null

function send(response: ServerResponse, status: number, value: unknown): void {
  response.writeHead(status, { 'Content-Type': 'application/json' })
  response.end(JSON.stringify(value))
}

beforeEach(async () => {
  server = createServer((request: IncomingMessage, response: ServerResponse) => {
    lastUrl = request.url ?? ''
    if (request.url === '/internal/v1/ask') {
      let body = ''
      request.on('data', chunk => { body += String(chunk) })
      request.on('end', () => { lastBody = JSON.parse(body); send(response, 200, { answer: 'Scoped', hits: [] }) })
      return
    }
    if (request.url === '/internal/v1/notes/note-1') {
      send(response, 200, { note_id: 'note-1', version_id: 'version-1', blocks: [{ block_key: 'b1', type: 'PARAGRAPH', text: 'Fact', assets: [{ page_label: '132' }], evidence: [{ page_label: '141', quote: 'Patch dimensions', evidence_level: 'MINERU_TEXT_MATCHED' }] }] })
      return
    }
  if (request.url?.startsWith('/internal/v1/search')) {
      send(response, 200, [{
        note_id: 'note-1', note_version_id: 'version-1', title: 'CNN', thesis: 'thesis',
        body: 'body', score: 0.9,
        evidence: [{ page_label: '2', quote: 'MLPs are not translation invariant', evidence_level: 'MINERU_TEXT_MATCHED' }],
        assets: [{ id: 'asset-1' }],
      }])
      return
    }
    if (request.url === '/internal/v1/source-notes') {
      send(response, 200, [{
        source_note_id: 'source-note-1', version_id: 'source-note-version-1', source_id: 'source-1',
        source_version_id: 'source-version-1', title: 'CNN', summary: 'summary', version_no: 3,
      }])
      return
    }
    if (request.url === '/internal/v1/source-notes/source-note-1') {
      send(response, 200, {
        source_note_id: 'source-note-1', version_id: 'source-note-version-1',
        source_version_id: 'source-version-1', source_file: 'cnn.pdf', title: 'CNN', summary: 'summary',
        version_no: 3, unit_count: 1,
        outline: [{ stable_key: 'intro', title: 'Introduction', level: 1, notes: [], children: [{
          stable_key: 'translation', title: 'Translation', level: 2, children: [],
          notes: [{ note_id: 'note-1', version_id: 'version-1', title: 'Translation invariance', thesis: 'thesis',
            evidence: [{ page_label: '2', quote: 'quote', evidence_level: 'MINERU_TEXT_MATCHED' }] }],
        }] }],
      })
      return
    }
    if (request.url === '/missing') {
      send(response, 404, { detail: 'not found' })
      return
    }
    send(response, 404, { detail: 'unknown route' })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('Mock server did not bind')
  baseUrl = `http://127.0.0.1:${address.port}`
})

afterEach(async () => {
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
})

async function context(): Promise<Context> {
  const ctx = new Context()
  await ctx.plugin(NoodleHttpProvider, { baseUrl, timeoutMs: 2_000, maxBodyChars: 1_000 })
  return ctx
}

describe('NoodleHttpProvider', () => {
  it('normalizes search evidence and assets', async () => {
    const ctx = await context()
    const result = await ctx.noodle.searchKnowledge('cnn', 5)
    expect(result).toMatchObject({ query: 'cnn', count: 1 })
    expect(result.hits[0]).toMatchObject({ noteId: 'note-1', assetCount: 1, score: 0.9 })
    expect(result.hits[0]?.evidence[0]?.pageLabel).toBe('2')
    await ctx.fiber.dispose()
  })

  it('flattens a nested SourceNote outline without copying full block bodies', async () => {
    const ctx = await context()
    const result = await ctx.noodle.getSourceNote('source-note-1')
    expect(result.outline).toHaveLength(2)
    expect(result.outline[1]).toMatchObject({ stableKey: 'translation', noteCount: 1 })
    expect(result.outline[1]?.notes[0]?.pages).toEqual(['2'])
    await ctx.fiber.dispose()
  })

  it('exposes a structured API error', async () => {
    const ctx = await context()
    await expect(ctx.noodle.getJobStatus('missing')).rejects.toMatchObject({
      name: 'NoodleApiError', status: 404,
    } satisfies Partial<NoodleApiError>)
    await ctx.fiber.dispose()
  })
})


describe('current source evidence contract', () => {
  it('forwards the selected source to search and answer', async () => {
    const ctx = await context()
    await ctx.noodle.searchKnowledge('Video Swin', 5, { sourceVersionId: 'ee6427-v2' })
    expect(new URL(lastUrl, baseUrl).searchParams.get('source_version_id')).toBe('ee6427-v2')
    await ctx.noodle.answerKnowledge('Patch size?', 5, { sourceVersionId: 'ee6427-v2' })
    expect(lastBody).toEqual({ question: 'Patch size?', limit: 5, source_version_id: 'ee6427-v2' })
    await ctx.fiber.dispose()
  })

  it('preserves block-level page evidence for direct note reads', async () => {
    const ctx = await context()
    const note = await ctx.noodle.getNote('note-1')
    expect(note.blocks[0]?.assetPages).toEqual(['132'])
    expect(note.blocks[0]?.evidence).toEqual([{ pageLabel: '141', quote: 'Patch dimensions', level: 'MINERU_TEXT_MATCHED' }])
    await ctx.fiber.dispose()
  })
})
