import { Context } from '@deepseek-ai/cordis'
import { CallId } from '@deepseek-ai/dsh-llm'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import * as Bridge from '../src/index.js'
import type { SearchResultView, SourceNoteListView, SourceNoteView } from '../src/contracts.js'

const baseUrl = process.env.NOODLE_API_BASE_URL ?? 'http://127.0.0.1:8000'
const ctx = new Context()
await ctx.plugin(SystemPrompt)
await ctx.plugin(ToolRuntime)
await ctx.plugin(Bridge, {
  baseUrl,
  apiPrefix: process.env.NOODLE_API_PREFIX ?? '/internal/v1',
  timeoutMs: Number(process.env.NOODLE_API_TIMEOUT_MS ?? 15_000),
  maxBodyChars: 4_000,
})

try {
  async function execute<T>(name: string, arguments_: Record<string, unknown>): Promise<T> {
    const result = await ctx.tools.execute({
      signal: new AbortController().signal,
      callId: CallId(`smoke-${name}`),
      name,
      arguments: arguments_,
    })
    if (result.isError) {
      const message = result.content.map(block => block.type === 'text' ? block.text : '').join('\n')
      throw new Error(`${name} failed: ${message}`)
    }
    return result.value as T
  }

  const notes = await execute<SourceNoteListView>('noodle_list_source_notes', { limit: 3 })
  if (notes.count < 1) throw new Error('No SourceNotes are available for the smoke test')
  const first = notes.notes[0]
  if (!first) throw new Error('SourceNote list was unexpectedly empty')
  const sourceNote = await execute<SourceNoteView>('noodle_get_source_note', { source_note_id: first.sourceNoteId })
  const flatten = (nodes: typeof sourceNote.outline): typeof sourceNote.outline =>
    nodes.flatMap(node => [node, ...flatten(node.children ?? [])])
  const firstAtomicNote = flatten(sourceNote.outline).flatMap(entry => entry.notes).find(note => note.title)
  const searchQuery = firstAtomicNote?.title ?? sourceNote.title
  const search = await execute<SearchResultView>('noodle_search_knowledge', { query: searchQuery, limit: 3 })
  if (sourceNote.sourceNoteId !== first.sourceNoteId) throw new Error('SourceNote id mismatch')
  if (search.count < 1) throw new Error('Knowledge search returned no hits')
  console.log(JSON.stringify({
    ok: true,
    baseUrl,
    registeredTools: ctx.tools.schemas().map(tool => tool.name).filter(name => name.startsWith('noodle_')).sort(),
    sourceNote: {
      id: sourceNote.sourceNoteId,
      title: sourceNote.title,
      unitCount: sourceNote.unitCount,
      outlineEntries: sourceNote.outline.length,
    },
    search: {
      query: search.query,
      count: search.count,
      firstHit: search.hits[0]?.title ?? '',
    },
  }, null, 2))
} finally {
  await ctx.fiber.dispose()
}
