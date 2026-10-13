import { Context } from '@deepseek-ai/cordis'
import { CallId } from '@deepseek-ai/dsh-llm'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import { describe, expect, it } from 'vitest'
import type {
  AnswerResultView,
  JobStatusView,
  RequestContext,
  SearchResultView,
  SourceNoteListView,
  SourceNoteView,
  SynthesisListView,
  SynthesisView,
  ConceptView,
  ConceptCandidateView,
  ConceptContributionView,
  ConceptUpdateJobView,
  ConceptRegistryView,
} from '../src/contracts.js'
import * as ReadTools from '../src/read-tools.js'
import * as WriteTools from '../src/write-tools.js'
import { NoodleService } from '../src/service.js'

class FakeNoodleService extends NoodleService {
  searchKnowledge(query: string, _limit: number, _context?: RequestContext): Promise<SearchResultView> {
    return Promise.resolve({ query, count: 0, hits: [] })
  }
  answerKnowledge(question: string): Promise<AnswerResultView> {
    return Promise.resolve({ question, answer: 'grounded answer', coverageWarning: '', citations: [] })
  }
  listSourceNotes(): Promise<SourceNoteListView> {
    return Promise.resolve({ count: 1, notes: [{
      sourceNoteId: 'sn-1', versionId: 'snv-1', sourceId: 's-1', sourceVersionId: 'sv-1',
      title: 'CNN', summary: 'summary', versionNo: 1,
    }] })
  }
  getSourceNote(sourceNoteId: string): Promise<SourceNoteView> {
    return Promise.resolve({ sourceNoteId, versionId: 'snv-1', sourceVersionId: 'sv-1', sourceFile: 'cnn.pdf',
      title: 'CNN', summary: 'summary', versionNo: 1, unitCount: 0, outline: [] })
  }
  getJobStatus(jobId: string): Promise<JobStatusView> {
    return Promise.resolve({ jobId, sourceVersionId: 'sv-1', status: 'COMPLETED', stage: 'COMPLETED',
      progress: 1, warnings: [], errorCode: '', errorDetail: '', updatedAt: '2026-01-01T00:00:00Z',
      harnessSessionId: '', harnessToolCallId: '' })
  }
  getNote(noteId: string) {
    return Promise.resolve({ noteId, versionId: 'nv-1', title: 'CNN', thesis: 'thesis', quality: 'AUTO_VALIDATED',
      review: 'UNREVIEWED', blocks: [] })
  }
  listSyntheses(): Promise<SynthesisListView> {
    return Promise.resolve({ count: 1, syntheses: [{ synthesisId: 'sy-1', versionId: 'syv-1',
      title: '机器学习', description: 'backbone', status: 'SEEDED', conceptCount: 1, coverage: 1 }] })
  }
  getSynthesis(synthesisId: string): Promise<SynthesisView> {
    return Promise.resolve({ synthesisId, versionId: 'syv-1', title: '机器学习', description: 'backbone',
      status: 'SEEDED', outline: [{ nodeId: 'n-1', stableKey: 'ml:cnn', title: '卷积神经网络',
        originalTitle: 'CNN', level: 2, nodeKind: 'CONCEPT', conceptId: 'c-1',
        conceptVersionId: 'cv-1', pages: [10, 20] }] })
  }
  getConcept(conceptId: string): Promise<ConceptView> {
    return Promise.resolve({ conceptId, versionId: 'cv-1', title: '卷积神经网络', originalTitle: 'CNN',
      scope: 'image models', summary: 'summary', status: 'SEEDED', blocks: [] })
  }
  listConcepts(): Promise<ConceptRegistryView> {
    return Promise.resolve({ count: 1, concepts: [{ conceptId: 'c-1', stableKey: 'ml:cnn',
      versionId: 'cv-1', versionNo: 1, title: '卷积神经网络', summary: 'summary', aliases: ['CNN'],
      synthesisPaths: ['机器学习 / 深度学习 / 卷积神经网络'], synthesisCount: 1 }] })
  }
  suggestConceptCandidates(): Promise<ConceptCandidateView[]> { return Promise.resolve([]) }
  listConceptContributions(): Promise<ConceptContributionView[]> { return Promise.resolve([]) }
  getConceptUpdateJob(jobId: string): Promise<ConceptUpdateJobView> {
    return Promise.resolve({ jobId, status: 'PUBLISHED', operation: 'ADD', conceptId: 'c-1',
      moduleKey: 'overview', candidateConceptVersionId: 'cv-2', errorCode: '', errorDetail: '',
      attemptCount: 1, judges: [] })
  }
  acceptAtomicNoteContribution() { throw new Error('write backend must not run without approval') }
  transitionConceptContribution() { throw new Error('write backend must not run without approval') }
  createSynthesis() { throw new Error('write backend must not run without approval') }
  publishSynthesis() { throw new Error('write backend must not run without approval') }
  mergeConcepts() { throw new Error('write backend must not run without approval') }
  ingestPdf() { throw new Error('write backend must not run without approval') }
  uploadImageAttachment() { throw new Error('write backend must not run without approval') }
  restartGeneration() { throw new Error('write backend must not run without approval') }
  editNoteBlock() { throw new Error('write backend must not run without approval') }
  replaceSourceImages() { throw new Error('write backend must not run without approval') }
  exportMarkdown() { throw new Error('write backend must not run without approval') }
}

async function setup(): Promise<Context> {
  const ctx = new Context()
  await ctx.plugin(SystemPrompt)
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(FakeNoodleService)
  await ctx.plugin(ReadTools)
  return ctx
}

describe('Noodle read tools', () => {
  it('registers the thirteen read-only tools', async () => {
    const ctx = await setup()
    expect(ctx.tools.schemas().map(tool => tool.name).sort()).toEqual([
      'noodle_answer_knowledge',
      'noodle_get_atomic_note',
      'noodle_get_concept',
      'noodle_get_concept_update_job',
      'noodle_get_job_status',
      'noodle_get_source_note',
      'noodle_get_synthesis',
      'noodle_list_concept_contributions',
      'noodle_list_concepts',
      'noodle_list_source_notes',
      'noodle_list_syntheses',
      'noodle_search_knowledge',
      'noodle_suggest_concept_candidates',
    ])
    await ctx.fiber.dispose()
  })

  it('executes through the real Harness tool runtime and preserves canonical JSON', async () => {
    const ctx = await setup()
    const result = await ctx.tools.execute({
      signal: new AbortController().signal,
      callId: CallId('test-call'),
      name: 'noodle_list_source_notes',
      arguments: { limit: 10 },
    })
    expect(result.isError).toBe(false)
    expect(result.value).toMatchObject({ count: 1 })
    expect(result.content[0]).toMatchObject({ type: 'text' })
    await ctx.fiber.dispose()
  })

  it('rejects blank search input before an API call', async () => {
    const ctx = await setup()
    const result = await ctx.tools.execute({
      signal: new AbortController().signal,
      callId: CallId('blank-search'),
      name: 'noodle_search_knowledge',
      arguments: { query: '   ' },
    })
    expect(result.isError).toBe(true)
    await ctx.fiber.dispose()
  })
})

describe('Noodle write tools', () => {
  it('registers eleven exclusive mutation tools and fails closed without approval support', async () => {
    const ctx = await setup()
    await ctx.plugin(WriteTools)
    const names = ctx.tools.schemas().map(tool => tool.name)
    expect(names.filter(name => name.startsWith('noodle_') && WriteTools.WRITE_TOOL_NAMES.has(name)).sort()).toEqual([
      'noodle_add_atomic_note_to_concept',
      'noodle_create_synthesis',
      'noodle_edit_note_block',
      'noodle_export_markdown',
      'noodle_ingest_pdf',
      'noodle_merge_concepts',
      'noodle_publish_synthesis',
      'noodle_replace_source_images',
      'noodle_restart_generation',
      'noodle_transition_concept_contribution',
      'noodle_upload_image_attachment',
    ])
    const result = await ctx.tools.execute({
      signal: new AbortController().signal,
      callId: CallId('unapproved-write'),
      name: 'noodle_ingest_pdf',
      arguments: { file_path: 'D:/private.pdf' },
    })
    expect(result.isError).toBe(true)
    expect(result.content[0]).toMatchObject({ type: 'text' })
    await ctx.fiber.dispose()
  })
})
