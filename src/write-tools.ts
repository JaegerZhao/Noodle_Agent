import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { SynthesisOutlineDraft } from './contracts.js'
import type {} from './service.js'

export const name = 'noodle-write-tools'
export const inject = ['tools', 'noodle']

export const WRITE_TOOL_NAMES = new Set([
  'noodle_ingest_pdf',
  'noodle_upload_image_attachment',
  'noodle_restart_generation',
  'noodle_edit_note_block',
  'noodle_replace_source_images',
  'noodle_export_markdown',
  'noodle_add_atomic_note_to_concept',
  'noodle_transition_concept_contribution',
  'noodle_create_synthesis',
  'noodle_publish_synthesis',
  'noodle_merge_concepts',
])

const textResult = (_args: unknown, value: unknown) => [
  { type: 'text' as const, text: JSON.stringify(value, null, 2) },
]

function requestContext(exec: {
  signal: AbortSignal
  callId: unknown
  agent?: { session: { id: unknown } }
}) {
  return {
    signal: exec.signal,
    callId: String(exec.callId),
    ...(exec.agent ? { sessionId: String(exec.agent.session.id) } : {}),
  }
}

const jobOutput = {
  type: 'object' as const,
  additionalProperties: false,
  properties: {
    jobId: { type: 'string' as const, required: true },
    sourceVersionId: { type: 'string' as const, required: true },
    status: { type: 'string' as const, required: true },
    stage: { type: 'string' as const, required: true },
    progress: { type: 'number' as const, required: true },
    warnings: { type: 'array' as const, required: true, items: { type: 'string' as const } },
    errorCode: { type: 'string' as const, required: true },
    errorDetail: { type: 'string' as const, required: true },
    updatedAt: { type: 'string' as const, required: true },
    harnessSessionId: { type: 'string' as const, required: true },
    harnessToolCallId: { type: 'string' as const, required: true },
  },
} as const

function parseOutlineJson(value: string): SynthesisOutlineDraft[] {
  const parsed: unknown = JSON.parse(value)
  if (!Array.isArray(parsed) || parsed.length === 0) throw new Error('outline_json must be a non-empty JSON array')
  let count = 0
  const visit = (raw: unknown): SynthesisOutlineDraft => {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Each outline node must be an object')
    const row = raw as Record<string, unknown>
    const title = typeof row.title === 'string' ? row.title.trim() : ''
    const nodeKind = row.node_kind === 'GROUP' ? 'GROUP' : row.node_kind === 'CONCEPT' ? 'CONCEPT' : ''
    const conceptId = typeof row.concept_id === 'string' ? row.concept_id.trim() : ''
    if (!title || !nodeKind) throw new Error('Each outline node requires title and node_kind=GROUP|CONCEPT')
    if (nodeKind === 'CONCEPT' && !conceptId) throw new Error(`Concept node "${title}" requires concept_id`)
    count += 1
    if (count > 200) throw new Error('outline_json cannot exceed 200 nodes')
    const children = Array.isArray(row.children) ? row.children.map(visit) : []
    return {
      ...(typeof row.stable_key === 'string' && row.stable_key.trim() ? { stableKey: row.stable_key.trim() } : {}),
      title,
      nodeKind,
      ...(conceptId ? { conceptId } : {}),
      children,
    }
  }
  return parsed.map(visit)
}

const synthesisPublishOutput = {
  type: 'object' as const,
  additionalProperties: false,
  properties: {
    status: { type: 'string' as const, required: true },
    synthesisId: { type: 'string' as const, required: true },
    versionId: { type: 'string' as const, required: true },
    versionNo: { type: 'integer' as const, required: true },
    conceptCount: { type: 'integer' as const, required: true },
  },
} as const

/** Register reversible domain mutations. The pre-execute hook is the approval boundary. */
export function apply(ctx: Context): void {
  ctx.on('tools/pre-execute', async (exec, next) => {
    if (!WRITE_TOOL_NAMES.has(exec.name)) return next()
    return {
      kind: 'ask',
      reason: `Noodle 写操作 ${exec.name} 将创建任务、附件、导出文件，或发布新的不可变 Note / Concept / Synthesis 版本。`,
    }
  })

  ctx.tools.register(defineTool({
    name: 'noodle_ingest_pdf',
    description: 'Upload a local PDF into Noodle and create a background processing job. Requires user approval.',
    parameters: {
      file_path: { type: 'string', required: true, description: 'Absolute local path to a PDF.' },
      title: { type: 'string', description: 'Optional source title.' },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          sourceId: { type: 'string', required: true },
          sourceVersionId: { type: 'string', required: true },
          jobId: { type: 'string', required: true },
          deduplicated: { type: 'string', required: true },
          jobUrl: { type: 'string', required: true },
          timelineUrl: { type: 'string', required: true },
          resultUrl: { type: 'string', required: true },
        },
      },
      render: textResult,
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const filePath = args.file_path.trim()
      if (!filePath.toLowerCase().endsWith('.pdf')) throw new Error('file_path must point to a PDF')
      const title = args.title?.trim() || undefined
      return ctx.noodle.ingestPdf(filePath, title, requestContext(exec))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'noodle_upload_image_attachment',
    description: 'Upload a local PNG/JPEG/WebP as a reusable image-edit attachment. Requires user approval.',
    parameters: {
      file_path: { type: 'string', required: true, description: 'Absolute local image path.' },
      analyze: { type: 'boolean', description: 'Run the configured vision model. Defaults to true.' },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          attachmentId: { type: 'string', required: true },
          originalName: { type: 'string', required: true },
          status: { type: 'string', required: true },
          mediaType: { type: 'string', required: true },
          byteSize: { type: 'integer', required: true },
          blobUrl: { type: 'string', required: true },
          visionJson: { type: 'string', required: true },
        },
      },
      render: textResult,
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const filePath = args.file_path.trim()
      if (!filePath) throw new Error('file_path must not be blank')
      return ctx.noodle.uploadImageAttachment(filePath, args.analyze ?? true, requestContext(exec))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'noodle_restart_generation',
    description: 'Restart only the requested generation suffix while reusing the successful MinerU parse. Requires user approval.',
    parameters: {
      job_id: { type: 'string', required: true },
      from_step: {
        type: 'string', required: true,
        description: 'One of profile, knowledge-map, outline, note-set, vision, drafts, evaluation, validation.',
      },
    },
    output: { schema: jobOutput, render: textResult },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const allowed = new Set(['profile', 'knowledge-map', 'outline', 'note-set', 'vision', 'drafts', 'evaluation', 'validation'])
      if (!allowed.has(args.from_step)) throw new Error(`Unsupported from_step: ${args.from_step}`)
      return ctx.noodle.restartGeneration(args.job_id.trim(), args.from_step, requestContext(exec))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'noodle_edit_note_block',
    description: 'Replace one non-image AtomicNote block and publish a new immutable AtomicNote + SourceNote version. Read the note first. Requires user approval.',
    parameters: {
      note_id: { type: 'string', required: true },
      expected_version_id: { type: 'string', required: true, description: 'Active version read immediately before editing.' },
      block_key: { type: 'string', required: true },
      new_markdown: { type: 'string', required: true, description: 'Complete replacement Markdown for this block.' },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          status: { type: 'string', required: true },
          noteId: { type: 'string', required: true },
          previousVersionId: { type: 'string', required: true },
          versionId: { type: 'string', required: true },
          versionNo: { type: 'integer', required: true },
          sourceNoteId: { type: 'string', required: true },
          sourceNoteVersionId: { type: 'string', required: true },
          blockKey: { type: 'string', required: true },
          origin: { type: 'string', required: true },
        },
      },
      render: textResult,
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (!args.new_markdown.trim()) throw new Error('new_markdown must not be blank')
      return ctx.noodle.editNoteBlock(
        args.note_id.trim(),
        args.expected_version_id.trim(),
        args.block_key.trim(),
        args.new_markdown,
        requestContext(exec),
      )
    },
  }))

  ctx.tools.register(defineTool({
    name: 'noodle_replace_source_images',
    description: 'Replace active source-note images from a PDF page range with one uploaded attachment, preserving version history. Requires user approval.',
    parameters: {
      source_note_id: { type: 'string', required: true },
      expected_source_note_version_id: { type: 'string', required: true },
      attachment_id: { type: 'string', required: true },
      page_start: { type: 'integer', required: true },
      page_end: { type: 'integer', required: true },
      target_note_title: { type: 'string' },
      caption: { type: 'string' },
      purpose: { type: 'string' },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          status: { type: 'string', required: true },
          sourceNoteId: { type: 'string', required: true },
          sourceNoteVersionId: { type: 'string', required: true },
          previousSourceNoteVersionId: { type: 'string', required: true },
          attachmentId: { type: 'string', required: true },
          visualAssetId: { type: 'string', required: true },
          removedVisualCount: { type: 'integer', required: true },
          updatedNotes: {
            type: 'array', required: true,
            items: {
              type: 'object', additionalProperties: false,
              properties: {
                noteId: { type: 'string', required: true },
                title: { type: 'string', required: true },
                previousVersionId: { type: 'string', required: true },
                versionId: { type: 'string', required: true },
                versionNo: { type: 'integer', required: true },
                containsReplacement: { type: 'boolean', required: true },
              },
            },
          },
        },
      },
      render: textResult,
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (args.page_start < 1 || args.page_end < args.page_start) throw new Error('Invalid page range')
      return ctx.noodle.replaceSourceImages(
        args.source_note_id.trim(),
        {
          attachmentId: args.attachment_id.trim(),
          pageStart: args.page_start,
          pageEnd: args.page_end,
          expectedSourceNoteVersionId: args.expected_source_note_version_id.trim(),
          ...(args.target_note_title?.trim() ? { targetNoteTitle: args.target_note_title.trim() } : {}),
          ...(args.caption?.trim() ? { caption: args.caption.trim() } : {}),
          ...(args.purpose?.trim() ? { purpose: args.purpose.trim() } : {}),
        },
        requestContext(exec),
      )
    },
  }))

  ctx.tools.register(defineTool({
    name: 'noodle_add_atomic_note_to_concept',
    description: 'Queue one active AtomicNote version for a durable Concept module update. A Worker generates, validates, independently judges, and publishes a new Concept/Synthesis release. Poll noodle_get_concept_update_job; failures keep the current head unchanged. Read both entities first. Requires user approval.',
    parameters: {
      concept_id: { type: 'string', required: true },
      expected_concept_version_id: { type: 'string', required: true },
      module_key: { type: 'string', required: true, description: 'Existing active Concept module key from noodle_get_concept.' },
      note_version_id: { type: 'string', required: true, description: 'Current AtomicNote version from noodle_get_atomic_note.' },
      rationale: { type: 'string', required: true, description: 'Why this AtomicNote belongs in this exact Concept module.' },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          status: { type: 'string', required: true },
          conceptId: { type: 'string', required: true },
          conceptVersionId: { type: 'string', required: true },
          candidateConceptVersionId: { type: 'string', required: true },
          contributionId: { type: 'string', required: true },
          updateJobId: { type: 'string', required: true },
          noteVersionId: { type: 'string', required: true },
          moduleKey: { type: 'string', required: true },
          reused: { type: 'boolean', required: true },
          synthesisVersions: { type: 'array', required: true, items: { type: 'string' } },
        },
      },
      render: textResult,
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (!args.rationale.trim()) throw new Error('rationale must not be blank')
      return ctx.noodle.acceptAtomicNoteContribution({
        conceptId: args.concept_id.trim(),
        expectedConceptVersionId: args.expected_concept_version_id.trim(),
        moduleKey: args.module_key.trim(),
        noteVersionId: args.note_version_id.trim(),
        rationale: args.rationale.trim(),
      }, requestContext(exec))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'noodle_transition_concept_contribution',
    description: 'Reject an approved Contribution, retract an accepted source, or supersede it with a newer active AtomicNote version. RETRACT and SUPERSEDE enqueue a durable module rebuild. Read the current Concept and Contribution first. Requires user approval.',
    parameters: {
      contribution_id: { type: 'string', required: true },
      action: { type: 'string', required: true, enum: ['REJECT', 'RETRACT', 'SUPERSEDE'] },
      expected_concept_version_id: { type: 'string', required: true },
      rationale: { type: 'string', required: true },
      replacement_note_version_id: { type: 'string', description: 'Required only for SUPERSEDE.' },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          status: { type: 'string', required: true },
          action: { type: 'string', required: true },
          conceptId: { type: 'string', required: true },
          contributionId: { type: 'string', required: true },
          previousContributionId: { type: 'string', required: true },
          updateJobId: { type: 'string', required: true },
          moduleKey: { type: 'string', required: true },
        },
      },
      render: textResult,
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (!args.rationale.trim()) throw new Error('rationale must not be blank')
      if (args.action === 'SUPERSEDE' && !args.replacement_note_version_id?.trim()) {
        throw new Error('replacement_note_version_id is required for SUPERSEDE')
      }
      return ctx.noodle.transitionConceptContribution({
        contributionId: args.contribution_id.trim(),
        action: args.action,
        expectedConceptVersionId: args.expected_concept_version_id.trim(),
        rationale: args.rationale.trim(),
        ...(args.replacement_note_version_id?.trim()
          ? { replacementNoteVersionId: args.replacement_note_version_id.trim() }
          : {}),
      }, requestContext(exec))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'noodle_create_synthesis',
    description: 'Create and immediately publish a user-curated knowledge Synthesis from existing global Concepts. Call noodle_list_concepts first and reuse identities instead of inventing duplicates. Requires user approval.',
    parameters: {
      stable_key: { type: 'string', required: true, description: 'Unique lowercase kebab-case identity, for example large-language-models.' },
      domain_key: { type: 'string', required: true },
      title: { type: 'string', required: true },
      description: { type: 'string', required: true },
      outline_json: { type: 'string', required: true, description: 'JSON array of nested {stable_key,title,node_kind,concept_id?,children}. GROUP has children; CONCEPT must reference an existing Concept UUID.' },
    },
    output: { schema: synthesisPublishOutput, render: textResult },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      return ctx.noodle.createSynthesis({
        stableKey: args.stable_key.trim(),
        domainKey: args.domain_key.trim(),
        title: args.title.trim(),
        description: args.description.trim(),
        outline: parseOutlineJson(args.outline_json),
      }, requestContext(exec))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'noodle_publish_synthesis',
    description: 'Publish a new immutable version of an existing user-curated Synthesis. Read its current version immediately before writing; stale expected_version_id is rejected. Requires user approval.',
    parameters: {
      synthesis_id: { type: 'string', required: true },
      expected_version_id: { type: 'string', required: true },
      title: { type: 'string', required: true },
      description: { type: 'string', required: true },
      outline_json: { type: 'string', required: true, description: 'Complete replacement outline JSON; omitted nodes are removed from the new release.' },
    },
    output: { schema: synthesisPublishOutput, render: textResult },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      return ctx.noodle.publishSynthesis(args.synthesis_id.trim(), {
        expectedVersionId: args.expected_version_id.trim(),
        title: args.title.trim(),
        description: args.description.trim(),
        outline: parseOutlineJson(args.outline_json),
      }, requestContext(exec))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'noodle_merge_concepts',
    description: 'Merge one duplicate Concept into a canonical target without discarding source modules. Creates a new target version, records aliases/audit, redirects the source, and refreshes every affected Synthesis. Read both current versions and explain the identity evidence first. Requires user approval.',
    parameters: {
      source_concept_id: { type: 'string', required: true },
      target_concept_id: { type: 'string', required: true },
      expected_source_version_id: { type: 'string', required: true },
      expected_target_version_id: { type: 'string', required: true },
      rationale: { type: 'string', required: true },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: {
          status: { type: 'string', required: true },
          sourceConceptId: { type: 'string', required: true },
          targetConceptId: { type: 'string', required: true },
          targetVersionId: { type: 'string', required: true },
          refreshedSynthesisVersions: { type: 'array', required: true, items: { type: 'string' } },
        },
      },
      render: textResult,
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (!args.rationale.trim()) throw new Error('rationale must not be blank')
      return ctx.noodle.mergeConcepts({
        sourceConceptId: args.source_concept_id.trim(),
        targetConceptId: args.target_concept_id.trim(),
        expectedSourceVersionId: args.expected_source_version_id.trim(),
        expectedTargetVersionId: args.expected_target_version_id.trim(),
        rationale: args.rationale.trim(),
      }, requestContext(exec))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'noodle_export_markdown',
    description: 'Materialize the active immutable note release as local Markdown files. Requires user approval because it writes files.',
    parameters: {
      source_version_id: { type: 'string', description: 'Optional source version; omit to export all active releases.' },
    },
    output: {
      schema: {
        type: 'array',
        items: {
          type: 'object', additionalProperties: false,
          properties: {
            sourceVersionId: { type: 'string', required: true },
            destination: { type: 'string', required: true },
            indexPath: { type: 'string', required: true },
            noteCount: { type: 'integer', required: true },
            sourceNotePath: { type: 'string', required: true },
          },
        },
      },
      render: textResult,
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      return ctx.noodle.exportMarkdown(args.source_version_id?.trim() || undefined, requestContext(exec))
    },
  }))
}
