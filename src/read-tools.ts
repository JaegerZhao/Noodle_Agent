import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type {} from './service.js'

export const name = 'noodle-read-tools'
export const inject = ['tools', 'noodle']

const evidenceSchema = {
  type: 'object' as const,
  additionalProperties: false,
  properties: {
    pageLabel: { type: 'string' as const, required: true },
    quote: { type: 'string' as const, required: true },
    level: { type: 'string' as const, required: true },
  },
} as const

const searchHitSchema = {
  type: 'object' as const,
  additionalProperties: false,
  properties: {
    entityType: { type: 'string' as const, required: true, enum: ['ATOMIC_NOTE', 'CONCEPT'] },
    noteId: { type: 'string' as const, required: true },
    noteVersionId: { type: 'string' as const, required: true },
    conceptId: { type: 'string' as const, required: true },
    synthesisPaths: { type: 'array' as const, required: true, items: { type: 'string' as const } },
    title: { type: 'string' as const, required: true },
    thesis: { type: 'string' as const, required: true },
    body: { type: 'string' as const, required: true },
    score: { type: 'number' as const, required: true },
    evidence: { type: 'array' as const, required: true, items: evidenceSchema },
    assetCount: { type: 'integer' as const, required: true },
  },
} as const

function renderJson(value: unknown) {
  return [{ type: 'text' as const, text: JSON.stringify(value, null, 2) }]
}

export function apply(ctx: Context, config: { nextOnly?: boolean } = {}): void {
  const available = (name: string) => !config.nextOnly || ['noodle_search_knowledge', 'noodle_answer_knowledge', 'noodle_get_atomic_note', 'noodle_list_concepts', 'noodle_get_concept'].includes(name)
  if (available('noodle_search_knowledge')) ctx.tools.register(defineTool({
    name: 'noodle_search_knowledge',
    description: 'Search one unified Noodle knowledge index containing reusable Concepts and source-grounded AtomicNotes. Use Concept hits for canonical explanations and AtomicNote hits for precise PDF evidence.',
    parameters: {
      query: { type: 'string', required: true, description: 'Natural-language search query.' },
      source_version_id: { type: 'string', description: 'For current-source questions, pass the selected source_version_id to exclude unrelated sources and Concepts.' },
      entity_note_ids: { type: 'array', items: { type: 'string' }, description: 'Known current note IDs resolving pronouns; never use previous answer text as evidence.' },
      limit: { type: 'integer', description: 'Maximum hits from 1 to 20.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          query: { type: 'string', required: true },
          status: { type: 'string' },
          coverageWarning: { type: 'string' },
          atomicCount: { type: 'integer' },
          conceptCount: { type: 'integer' },
          count: { type: 'integer', required: true },
          hits: { type: 'array', required: true, items: searchHitSchema },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const query = args.query.trim()
      if (!query) throw new Error('query must not be blank')
      const limit = Math.min(20, Math.max(1, args.limit ?? 5))
      return ctx.noodle.searchKnowledge(query, limit, {
        ...(args.source_version_id ? { sourceVersionId: args.source_version_id } : {}),
        ...(args.entity_note_ids ? { entityNoteIds: args.entity_note_ids } : {}),
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))

  if (available('noodle_answer_knowledge')) ctx.tools.register(defineTool({
    name: 'noodle_answer_knowledge',
    description: 'Answer a question using unified Concept + AtomicNote RAG and return citations tied to immutable versions and source pages when available.',
    parameters: {
      question: { type: 'string', required: true, description: 'Question grounded in the personal knowledge base.' },
      source_version_id: { type: 'string', description: 'For current-source questions, pass the selected source_version_id.' },
      entity_note_ids: { type: 'array', items: { type: 'string' }, description: 'Known current note IDs for pronoun resolution within the explicit scope.' },
      limit: { type: 'integer', description: 'Maximum retrieval hits from 1 to 20.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          question: { type: 'string', required: true },
          answer: { type: 'string', required: true },
          coverageWarning: { type: 'string', required: true },
          citations: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                noteId: { type: 'string', required: true },
                noteVersionId: { type: 'string', required: true },
                title: { type: 'string', required: true },
                score: { type: 'number', required: true },
                pages: { type: 'array', required: true, items: { type: 'string' } },
              },
            },
          },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const question = args.question.trim()
      if (!question) throw new Error('question must not be blank')
      const limit = Math.min(20, Math.max(1, args.limit ?? 5))
      return ctx.noodle.answerKnowledge(question, limit, {
        ...(args.source_version_id ? { sourceVersionId: args.source_version_id } : {}),
        ...(args.entity_note_ids ? { entityNoteIds: args.entity_note_ids } : {}),
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))

  if (available('noodle_list_source_notes')) ctx.tools.register(defineTool({
    name: 'noodle_list_source_notes',
    description: 'List current assembled SourceNotes and their active immutable versions.',
    parameters: {
      limit: { type: 'integer', description: 'Maximum SourceNotes from 1 to 50.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          count: { type: 'integer', required: true },
          notes: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                sourceNoteId: { type: 'string', required: true },
                versionId: { type: 'string', required: true },
                sourceId: { type: 'string', required: true },
                sourceVersionId: { type: 'string', required: true },
                title: { type: 'string', required: true },
                summary: { type: 'string', required: true },
                versionNo: { type: 'integer', required: true },
              },
            },
          },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const limit = Math.min(50, Math.max(1, args.limit ?? 20))
      return ctx.noodle.listSourceNotes(limit, {
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))

  if (available('noodle_get_source_note')) ctx.tools.register(defineTool({
    name: 'noodle_get_source_note',
    description: 'Get one SourceNote summary with its flattened outline, atomic-note references, theses, and source pages.',
    parameters: {
      source_note_id: { type: 'string', required: true, description: 'SourceNote UUID returned by noodle_list_source_notes.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          sourceNoteId: { type: 'string', required: true },
          versionId: { type: 'string', required: true },
          sourceVersionId: { type: 'string', required: true },
          sourceFile: { type: 'string', required: true },
          title: { type: 'string', required: true },
          summary: { type: 'string', required: true },
          versionNo: { type: 'integer', required: true },
          unitCount: { type: 'integer', required: true },
          outline: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                stableKey: { type: 'string', required: true },
                title: { type: 'string', required: true },
                level: { type: 'integer', required: true },
                noteCount: { type: 'integer', required: true },
                notes: {
                  type: 'array',
                  required: true,
                  items: {
                    type: 'object',
                    additionalProperties: false,
                    properties: {
                      noteId: { type: 'string', required: true },
                      versionId: { type: 'string', required: true },
                      title: { type: 'string', required: true },
                      thesis: { type: 'string', required: true },
                      pages: { type: 'array', required: true, items: { type: 'string' } },
                    },
                  },
                },
              },
            },
          },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const sourceNoteId = args.source_note_id.trim()
      if (!sourceNoteId) throw new Error('source_note_id must not be blank')
      return ctx.noodle.getSourceNote(sourceNoteId, {
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))

  if (available('noodle_get_atomic_note')) ctx.tools.register(defineTool({
    name: 'noodle_get_atomic_note',
    description: 'Read one active atomic note including stable block keys. Call this before proposing a block edit.',
    parameters: {
      note_id: { type: 'string', required: true, description: 'AtomicNote UUID.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          noteId: { type: 'string', required: true },
          versionId: { type: 'string', required: true },
          title: { type: 'string', required: true },
          thesis: { type: 'string', required: true },
          quality: { type: 'string', required: true },
          review: { type: 'string', required: true },
          blocks: {
            type: 'array', required: true,
            items: {
              type: 'object', additionalProperties: false,
              properties: {
                blockKey: { type: 'string', required: true },
                type: { type: 'string', required: true },
                text: { type: 'string', required: true },
                origin: { type: 'string', required: true },
                assetCount: { type: 'integer', required: true },
                assetPages: { type: 'array', items: { type: 'string' } },
                evidence: { type: 'array', items: evidenceSchema },
              },
            },
          },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const noteId = args.note_id.trim()
      if (!noteId) throw new Error('note_id must not be blank')
      return ctx.noodle.getNote(noteId, {
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))

  if (available('noodle_get_job_status')) ctx.tools.register(defineTool({
    name: 'noodle_get_job_status',
    description: 'Read the current status, stage, progress, warnings, and error for a NoodleAgent processing job.',
    parameters: {
      job_id: { type: 'string', required: true, description: 'NoodleAgent job UUID.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          jobId: { type: 'string', required: true },
          sourceVersionId: { type: 'string', required: true },
          status: { type: 'string', required: true },
          stage: { type: 'string', required: true },
          progress: { type: 'number', required: true },
          warnings: { type: 'array', required: true, items: { type: 'string' } },
          errorCode: { type: 'string', required: true },
          errorDetail: { type: 'string', required: true },
          updatedAt: { type: 'string', required: true },
          harnessSessionId: { type: 'string', required: true },
          harnessToolCallId: { type: 'string', required: true },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const jobId = args.job_id.trim()
      if (!jobId) throw new Error('job_id must not be blank')
      return ctx.noodle.getJobStatus(jobId, {
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))

  if (available('noodle_list_syntheses')) ctx.tools.register(defineTool({
    name: 'noodle_list_syntheses',
    description: 'List all Noodle long-term knowledge Syntheses, including textbook-seeded and user-curated knowledge systems.',
    parameters: {},
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          count: { type: 'integer', required: true },
          syntheses: { type: 'array', required: true, items: {
            type: 'object', additionalProperties: false,
            properties: {
              synthesisId: { type: 'string', required: true },
              versionId: { type: 'string', required: true },
              title: { type: 'string', required: true },
              description: { type: 'string', required: true },
              status: { type: 'string', required: true },
              conceptCount: { type: 'integer', required: true },
              coverage: { type: 'number', required: true },
            },
          } },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(_args, exec) {
      return ctx.noodle.listSyntheses({
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))

  if (available('noodle_get_synthesis')) ctx.tools.register(defineTool({
    name: 'noodle_get_synthesis',
    description: 'Read one complete Synthesis directory and its Concept identifiers. Use this to navigate a knowledge domain.',
    parameters: {
      synthesis_id: { type: 'string', required: true, description: 'Synthesis UUID from noodle_list_syntheses.' },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          synthesisId: { type: 'string', required: true },
          versionId: { type: 'string', required: true },
          title: { type: 'string', required: true },
          description: { type: 'string', required: true },
          status: { type: 'string', required: true },
          outline: { type: 'array', required: true, items: {
            type: 'object', additionalProperties: false,
            properties: {
              nodeId: { type: 'string', required: true },
              stableKey: { type: 'string', required: true },
              title: { type: 'string', required: true },
              originalTitle: { type: 'string', required: true },
              level: { type: 'integer', required: true },
              nodeKind: { type: 'string', required: true },
              conceptId: { type: 'string', required: true },
              conceptVersionId: { type: 'string', required: true },
              pages: { type: 'array', required: true, items: { type: 'integer' } },
            },
          } },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const synthesisId = args.synthesis_id.trim()
      if (!synthesisId) throw new Error('synthesis_id must not be blank')
      return ctx.noodle.getSynthesis(synthesisId, {
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))

  if (available('noodle_list_concepts')) ctx.tools.register(defineTool({
    name: 'noodle_list_concepts',
    description: 'Search the global Concept registry before creating a Synthesis or proposing a merge. Returns aliases and every active Synthesis path that reuses each Concept.',
    parameters: {
      query: { type: 'string', description: 'Optional title, alias, stable key, or abbreviation such as CNN.' },
      limit: { type: 'integer', description: 'Maximum Concepts from 1 to 200.' },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          count: { type: 'integer', required: true },
          concepts: { type: 'array', required: true, items: {
            type: 'object', additionalProperties: false,
            properties: {
              conceptId: { type: 'string', required: true },
              stableKey: { type: 'string', required: true },
              versionId: { type: 'string', required: true },
              versionNo: { type: 'integer', required: true },
              title: { type: 'string', required: true },
              summary: { type: 'string', required: true },
              aliases: { type: 'array', required: true, items: { type: 'string' } },
              synthesisPaths: { type: 'array', required: true, items: { type: 'string' } },
              synthesisCount: { type: 'integer', required: true },
            },
          } },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const limit = Math.min(200, Math.max(1, args.limit ?? 50))
      return ctx.noodle.listConcepts(args.query?.trim() || '', limit, {
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))

  if (available('noodle_get_concept')) ctx.tools.register(defineTool({
    name: 'noodle_get_concept',
    description: 'Read one Concept version, including structured blocks and source references. Pass the Synthesis-pinned version when navigating a release.',
    parameters: {
      concept_id: { type: 'string', required: true, description: 'Concept UUID from noodle_get_synthesis.' },
      version_id: { type: 'string', description: 'Optional pinned ConceptVersion UUID from noodle_get_synthesis.' },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          conceptId: { type: 'string', required: true },
          versionId: { type: 'string', required: true },
          title: { type: 'string', required: true },
          originalTitle: { type: 'string', required: true },
          scope: { type: 'string', required: true },
          summary: { type: 'string', required: true },
          status: { type: 'string', required: true },
          blocks: { type: 'array', required: true, items: {
            type: 'object', additionalProperties: false,
            properties: {
              blockKey: { type: 'string', required: true },
              type: { type: 'string', required: true },
              content: { type: 'string', required: true },
              sourceRole: { type: 'string', required: true },
              sourceRefs: { type: 'array', required: true, items: { type: 'string' } },
            },
          } },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const conceptId = args.concept_id.trim()
      if (!conceptId) throw new Error('concept_id must not be blank')
      return ctx.noodle.getConcept(conceptId, {
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      }, args.version_id?.trim() || undefined)
    },
  }))

  if (available('noodle_suggest_concept_candidates')) ctx.tools.register(defineTool({
    name: 'noodle_suggest_concept_candidates',
    description: 'Retrieve AtomicNote candidates for one existing Concept module. Results are proposals only and never authorize a write.',
    parameters: {
      concept_id: { type: 'string', required: true },
      module_key: { type: 'string', required: true },
      limit: { type: 'integer' },
    },
    output: {
      schema: {
        type: 'array',
        items: {
          type: 'object', additionalProperties: false,
          properties: {
            noteVersionId: { type: 'string', required: true },
            title: { type: 'string', required: true },
            thesis: { type: 'string', required: true },
            score: { type: 'number', required: true },
            pages: { type: 'array', required: true, items: { type: 'integer' } },
            evidenceCount: { type: 'integer', required: true },
            assetCount: { type: 'integer', required: true },
          },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const limit = args.limit ?? 8
      if (limit < 1 || limit > 20) throw new Error('limit must be between 1 and 20')
      return ctx.noodle.suggestConceptCandidates(
        args.concept_id.trim(),
        args.module_key.trim(),
        limit,
        {
          signal: exec.signal,
          callId: String(exec.callId),
          ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
        },
      )
    },
  }))

  if (available('noodle_list_concept_contributions')) ctx.tools.register(defineTool({
    name: 'noodle_list_concept_contributions',
    description: 'List every source Contribution and lifecycle state for one Concept before changing or removing knowledge.',
    parameters: {
      concept_id: { type: 'string', required: true },
    },
    output: {
      schema: {
        type: 'array',
        items: {
          type: 'object', additionalProperties: false,
          properties: {
            contributionId: { type: 'string', required: true },
            moduleKey: { type: 'string', required: true },
            noteVersionId: { type: 'string', required: true },
            noteTitle: { type: 'string', required: true },
            role: { type: 'string', required: true },
            status: { type: 'string', required: true },
            rationale: { type: 'string', required: true },
          },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      return ctx.noodle.listConceptContributions(args.concept_id.trim(), {
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))

  if (available('noodle_get_concept_update_job')) ctx.tools.register(defineTool({
    name: 'noodle_get_concept_update_job',
    description: 'Read the durable Worker state, Judge result, retry count, and failure detail for one asynchronous Concept update.',
    parameters: {
      job_id: { type: 'string', required: true },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          jobId: { type: 'string', required: true },
          status: { type: 'string', required: true },
          operation: { type: 'string', required: true },
          conceptId: { type: 'string', required: true },
          moduleKey: { type: 'string', required: true },
          candidateConceptVersionId: { type: 'string', required: true },
          errorCode: { type: 'string', required: true },
          errorDetail: { type: 'string', required: true },
          attemptCount: { type: 'integer', required: true },
          judges: { type: 'array', required: true, items: { type: 'object', additionalProperties: true } },
        },
      },
      render: (_args, value) => renderJson(value),
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      return ctx.noodle.getConceptUpdateJob(args.job_id.trim(), {
        signal: exec.signal,
        callId: String(exec.callId),
        ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
      })
    },
  }))
}
