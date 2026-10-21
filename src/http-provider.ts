import type { Context } from '@deepseek-ai/cordis'
import { readFile } from 'node:fs/promises'
import { basename } from 'node:path'
import type {
  AttachmentView,
  AnswerResultView,
  AtomicNoteView,
  AtomicNoteRef,
  BlockEditView,
  CitationView,
  EvidenceRef,
  ImageReplacementView,
  IngestPdfView,
  JobStatusView,
  MarkdownExportView,
  OutlineEntryView,
  RequestContext,
  SearchHitView,
  SearchResultView,
  SourceNoteListItem,
  SourceNoteListView,
  SourceNoteView,
  SynthesisListItemView,
  SynthesisListView,
  SynthesisOutlineEntryView,
  SynthesisView,
  ConceptView,
  ConceptContributionUpdateView,
  ConceptContributionTransitionView,
  ConceptCandidateView,
  ConceptContributionView,
  ConceptUpdateJobView,
  ConceptRegistryItemView,
  ConceptRegistryView,
  SynthesisOutlineDraft,
  SynthesisPublishView,
  ConceptMergeView,
} from './contracts.js'
import { NoodleService } from './service.js'

export interface HttpProviderConfig {
  baseUrl: string
  apiPrefix?: string
  retrievalMode?: "HYBRID" | "KEYWORD_ONLY"
  timeoutMs: number
  maxBodyChars: number
}

export class NoodleApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly path: string,
    readonly detail = '',
  ) {
    super(message)
    this.name = 'NoodleApiError'
  }
}

type JsonObject = Record<string, unknown>

function object(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : {}
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function string(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

function number(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function unique(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))]
}

function evidenceRefs(value: unknown): EvidenceRef[] {
  return array(value).map((item) => {
    const row = object(item)
    return {
      pageLabel: string(row.page_label),
      quote: string(row.quote),
      level: string(row.evidence_level),
    }
  })
}

function searchHit(value: unknown, maxBodyChars: number): SearchHitView {
  const row = object(value)
  const body = string(row.body)
  return {
    entityType: string(row.entity_type) === 'CONCEPT' ? 'CONCEPT' : 'ATOMIC_NOTE',
    noteId: string(row.note_id),
    noteVersionId: string(row.note_version_id),
    conceptId: string(row.concept_id),
    synthesisPaths: array(row.synthesis_paths).map(string),
    title: string(row.title),
    thesis: string(row.thesis),
    body: body.length > maxBodyChars ? `${body.slice(0, maxBodyChars)}\n…[truncated]` : body,
    score: number(row.score),
    evidence: evidenceRefs(row.evidence),
    assetCount: array(row.assets).length,
  }
}

function synthesisOutlinePayload(nodes: SynthesisOutlineDraft[]): unknown[] {
  return nodes.map(node => ({
    ...(node.stableKey?.trim() ? { stable_key: node.stableKey.trim() } : {}),
    title: node.title,
    node_kind: node.nodeKind,
    ...(node.conceptId?.trim() ? { concept_id: node.conceptId.trim() } : {}),
    children: synthesisOutlinePayload(node.children),
  }))
}

function synthesisPublish(value: unknown): SynthesisPublishView {
  const row = object(value)
  return {
    status: string(row.status),
    synthesisId: string(row.synthesis_id),
    versionId: string(row.version_id),
    versionNo: number(row.version_no),
    conceptCount: number(object(row.coverage).concept_nodes),
  }
}

function citation(value: unknown): CitationView {
  const row = object(value)
  const evidence = evidenceRefs(row.evidence)
  return {
    noteId: string(row.note_id),
    noteVersionId: string(row.note_version_id),
    title: string(row.title),
    score: number(row.score),
    pages: unique(evidence.map(item => item.pageLabel)),
  }
}

function atomicNoteRef(value: unknown): AtomicNoteRef {
  const row = object(value)
  const evidence = evidenceRefs(row.evidence)
  return {
    noteId: string(row.note_id),
    versionId: string(row.version_id),
    title: string(row.title),
    thesis: string(row.thesis),
    pages: unique([...evidence.map(item => item.pageLabel), ...array(row.assets).map(asset => string(object(asset).page_label)), ...array(row.blocks).flatMap(block => array(object(block).assets).map(asset => string(object(asset).page_label)))]).filter(Boolean),
  }
}

function flattenOutline(nodes: unknown, destination: OutlineEntryView[]): void {
  for (const item of array(nodes)) {
    const row = object(item)
    const notes = array(row.notes).map(atomicNoteRef)
    destination.push({
      stableKey: string(row.stable_key),
      title: string(row.title),
      level: number(row.level),
      noteCount: notes.length,
      notes,
    })
    flattenOutline(row.children, destination)
  }
}

function flattenSynthesisOutline(nodes: unknown, destination: SynthesisOutlineEntryView[]): void {
  for (const item of array(nodes)) {
    const row = object(item)
    const locator = object(row.source_locator)
    destination.push({
      nodeId: string(row.node_id),
      stableKey: string(row.stable_key),
      title: string(row.title),
      originalTitle: string(row.original_title),
      level: number(row.level),
      nodeKind: string(row.node_kind),
      conceptId: string(row.concept_id),
      conceptVersionId: string(row.concept_version_id),
      pages: array(locator.pages).map(number),
    })
    flattenSynthesisOutline(row.children, destination)
  }
}

/** HTTP provider. NoodleAgent remains the authority for domain data and policy. */
export class NoodleHttpProvider extends NoodleService {
  private readonly baseUrl: string
  private readonly apiPrefix: string

  constructor(ctx: Context, private readonly config: HttpProviderConfig) {
    super(ctx)
    this.baseUrl = config.baseUrl.replace(/\/+$/, '')
    const prefix = config.apiPrefix?.trim() || '/internal/v1'
    this.apiPrefix = `/${prefix.replace(/^\/+|\/+$/g, '')}`
  }

  private async request(path: string, init: RequestInit, context?: RequestContext): Promise<unknown> {
    const timeoutSignal = AbortSignal.timeout(this.config.timeoutMs)
    const signal = context?.signal
      ? AbortSignal.any([context.signal, timeoutSignal])
      : timeoutSignal
    const headers = new Headers(init.headers)
    headers.set('Accept', 'application/json')
    if (context?.callId) headers.set('X-Harness-Tool-Call-Id', context.callId)
    if (context?.sessionId) headers.set('X-Harness-Session-Id', context.sessionId)

    let response: Response
    try {
      response = await fetch(`${this.baseUrl}${this.apiPrefix}${path}`, {
        ...init,
        headers,
        signal,
      })
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      throw new NoodleApiError(`Noodle API request failed: ${detail}`, 0, path, detail)
    }

    const text = await response.text()
    let payload: unknown = null
    if (text) {
      try {
        payload = JSON.parse(text)
      } catch {
        payload = text
      }
    }
    if (!response.ok) {
      const detail = string(object(payload).detail) || string(payload)
      throw new NoodleApiError(
        `Noodle API ${response.status} for ${path}${detail ? `: ${detail}` : ''}`,
        response.status,
        path,
        detail,
      )
    }
    return payload
  }

  private async nextQuery(query: string, limit: number, context?: RequestContext): Promise<JsonObject> {
    let scope: JsonObject = { mode: 'GLOBAL' }
    if (context?.sourceVersionId) {
      const source = object(await this.request(`/source-revisions/${encodeURIComponent(context.sourceVersionId)}`, { method: 'GET' }, context))
      if (!string(source.source_id)) throw new Error('Selected source revision could not be resolved')
      scope = { mode: 'SOURCE', source_ids: [string(source.source_id)] }
    }
    return { query, top_k: limit, scope, allowed_kinds: context?.sourceVersionId ? ['ATOMIC'] : ['ATOMIC', 'CONCEPT'], retrieval_mode: this.config.retrievalMode ?? 'HYBRID', rerank: 'AUTO', entity_note_ids: context?.entityNoteIds ?? [] }
  }

  async searchKnowledge(query: string, limit: number, context?: RequestContext): Promise<SearchResultView> {
    if (this.apiPrefix === '/internal/next') {
      const payload = object(await this.request('/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(await this.nextQuery(query, limit, context)) }, context))
      const hits = array(payload.hits).map(item => { const hit = object(item); return searchHit({ entity_type: hit.kind, note_id: hit.note_id, note_version_id: hit.revision_id, concept_id: hit.kind === 'CONCEPT' ? hit.note_id : '', title: hit.title, thesis: hit.section_title, body: hit.body, score: hit.score, evidence: array(hit.provenance).map(value => ({ page_label: string(object(value).page), quote: '', evidence_level: 'PUBLISHED_NOTE' })) }, this.config.maxBodyChars) })
      return { query, count: hits.length, hits, status: string(payload.status), coverageWarning: string(payload.message), atomicCount: number(object(payload.counts).atomic_notes), conceptCount: number(object(payload.counts).concept_notes) }
    }
    const params = new URLSearchParams({ q: query, limit: String(limit) })
    if (context?.sourceVersionId) params.set("source_version_id", context.sourceVersionId)
    const payload = await this.request(`/search?${params}`, { method: 'GET' }, context)
    const hits = array(payload).map(item => searchHit(item, this.config.maxBodyChars))
    return { query, count: hits.length, hits }
  }

  async answerKnowledge(question: string, limit: number, context?: RequestContext): Promise<AnswerResultView> {
    if (this.apiPrefix === '/internal/next') {
      const payload = object(await this.request('/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(await this.nextQuery(question, limit, context)) }, context))
      return { question, answer: string(payload.answer_markdown), coverageWarning: string(payload.message) || (payload.status !== 'READY' ? string(payload.status) : ''), citations: array(payload.citations).map(value => { const row = object(value); return { noteId: string(row.note_id), noteVersionId: string(row.revision_id), title: string(row.title), score: number(row.score), pages: unique(array(row.provenance).map(p => string(object(p).page))).filter(Boolean) } }) }
    }
    const payload = object(await this.request('/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, limit, source_version_id: context?.sourceVersionId }),
    }, context))
    return {
      question,
      answer: string(payload.answer),
      coverageWarning: string(payload.coverage_warning),
      citations: array(payload.hits).map(citation),
    }
  }

  async listSourceNotes(limit: number, context?: RequestContext): Promise<SourceNoteListView> {
    const payload = array(await this.request('/source-notes', { method: 'GET' }, context))
    const notes: SourceNoteListItem[] = payload.slice(0, limit).map((item) => {
      const row = object(item)
      return {
        sourceNoteId: string(row.source_note_id),
        versionId: string(row.version_id),
        sourceId: string(row.source_id),
        sourceVersionId: string(row.source_version_id),
        title: string(row.title),
        summary: string(row.summary),
        versionNo: number(row.version_no),
      }
    })
    return { count: notes.length, notes }
  }

  async getSourceNote(sourceNoteId: string, context?: RequestContext): Promise<SourceNoteView> {
    const payload = object(await this.request(
      `/source-notes/${encodeURIComponent(sourceNoteId)}`,
      { method: 'GET' },
      context,
    ))
    const outline: OutlineEntryView[] = []
    flattenOutline(payload.outline, outline)
    return {
      sourceNoteId: string(payload.source_note_id),
      versionId: string(payload.version_id),
      sourceVersionId: string(payload.source_version_id),
      sourceFile: string(payload.source_file),
      title: string(payload.title),
      summary: string(payload.summary),
      versionNo: number(payload.version_no),
      unitCount: number(payload.unit_count),
      outline,
    }
  }

  async getJobStatus(jobId: string, context?: RequestContext): Promise<JobStatusView> {
    const payload = object(await this.request(
      `/jobs/${encodeURIComponent(jobId)}`,
      { method: 'GET' },
      context,
    ))
    const harness = object(payload.harness)
    return {
      jobId: string(payload.id) || jobId,
      sourceVersionId: string(payload.source_version_id),
      status: string(payload.status),
      stage: string(payload.stage),
      progress: number(payload.progress),
      warnings: array(payload.warnings).map(item => JSON.stringify(item)),
      errorCode: string(payload.error_code),
      errorDetail: string(payload.error_detail),
      updatedAt: string(payload.updated_at),
      harnessSessionId: string(harness.session_id),
      harnessToolCallId: string(harness.tool_call_id),
    }
  }

  async getNote(noteId: string, context?: RequestContext): Promise<AtomicNoteView> {
    const payload = object(await this.request(
      `/notes/${encodeURIComponent(noteId)}${this.apiPrefix === '/internal/next' && context?.sourceVersionId ? `?source_revision_id=${encodeURIComponent(context.sourceVersionId)}` : ''}`,
      { method: 'GET' },
      context,
    ))
    if (this.apiPrefix === '/internal/next') {
      const revision = object(payload.revision)
      const body = Array.from(string(payload.markdown))
      return { noteId: string(payload.id), versionId: string(revision.id), title: string(payload.title), thesis: string(revision.summary), quality: 'MODEL_REVIEWED', review: '', blocks: array(payload.sections).map(value => { const section = object(value); return { blockKey: string(section.section_id), type: 'MARKDOWN', text: body.slice(number(section.start_offset), number(section.end_offset)).join(''), origin: 'PUBLISHED_NEXT_REVISION', assetCount: 0, assetPages: array(section.provenance).map(ref => string(object(ref).page)), evidence: [] } }) }
    }
    return {
      noteId: string(payload.note_id),
      versionId: string(payload.version_id),
      title: string(payload.title),
      thesis: string(payload.thesis),
      quality: string(payload.quality),
      review: string(payload.review),
      blocks: array(payload.blocks).map((item) => {
        const row = object(item)
        return {
          blockKey: string(row.block_key),
          type: string(row.type),
          text: string(row.text) || string(row.caption) || string(row.purpose) || array(row.assets).map(asset => string(object(asset).caption) || string(object(asset).alt_text)).filter(Boolean).join("\n"),
          origin: string(row.origin),
          assetCount: array(row.assets).length,
          assetPages: unique(array(row.assets).map(asset => string(object(asset).page_label))).filter(Boolean),
          evidence: array(row.evidence).map(item => { const ref = object(item); return { pageLabel: string(ref.page_label), quote: string(ref.quote), level: string(ref.evidence_level) } }),
        }
      }),
    }
  }

  async listSyntheses(context?: RequestContext): Promise<SynthesisListView> {
    const payload = array(await this.request('/syntheses', { method: 'GET' }, context))
    const syntheses: SynthesisListItemView[] = payload.map((item) => {
      const row = object(item)
      const coverage = object(row.coverage)
      return {
        synthesisId: string(row.synthesis_id),
        versionId: string(row.version_id),
        title: string(row.title),
        description: string(row.description),
        status: string(row.status),
        conceptCount: number(coverage.concept_nodes),
        coverage: number(coverage.coverage),
      }
    })
    return { count: syntheses.length, syntheses }
  }

  async getSynthesis(synthesisId: string, context?: RequestContext): Promise<SynthesisView> {
    const payload = object(await this.request(
      `/syntheses/${encodeURIComponent(synthesisId)}`,
      { method: 'GET' },
      context,
    ))
    const outline: SynthesisOutlineEntryView[] = []
    flattenSynthesisOutline(payload.outline, outline)
    return {
      synthesisId: string(payload.synthesis_id),
      versionId: string(payload.version_id),
      title: string(payload.title),
      description: string(payload.description),
      status: string(payload.status),
      outline,
    }
  }

  async listConcepts(
    query: string,
    limit: number,
    context?: RequestContext,
  ): Promise<ConceptRegistryView> {
    if (this.apiPrefix === '/internal/next') {
      if (context?.sourceVersionId) throw new NoodleApiError('Source scope does not admit multi-source Concepts', 409, '/concepts')
      const payload = object(await this.request(`/concepts?q=${encodeURIComponent(query)}&limit=${limit}`, { method: 'GET' }, context))
      const concepts = array(payload.items).filter(value => object(value).eligible === true).map(value => { const row = object(value); return { conceptId: string(row.id), stableKey: string(row.id), versionId: string(row.current_revision_id), versionNo: 0, title: string(row.title), summary: '', aliases: array(JSON.parse(string(row.aliases_json) || '[]')).map(string), synthesisPaths: [], synthesisCount: 0 } })
      return { count: concepts.length, concepts }
    }
    const params = new URLSearchParams({ q: query, limit: String(limit) })
    if (context?.sourceVersionId) params.set("source_version_id", context.sourceVersionId)
    const rows = array(await this.request(`/concepts?${params}`, { method: 'GET' }, context))
    const concepts: ConceptRegistryItemView[] = rows.map((item) => {
      const row = object(item)
      return {
        conceptId: string(row.concept_id),
        stableKey: string(row.stable_key),
        versionId: string(row.version_id),
        versionNo: number(row.version_no),
        title: string(row.title),
        summary: string(row.summary),
        aliases: array(row.aliases).map(string),
        synthesisPaths: array(row.synthesis_paths).map(string),
        synthesisCount: number(row.synthesis_count),
      }
    })
    return { count: concepts.length, concepts }
  }

  async createSynthesis(
    input: {
      stableKey: string
      domainKey: string
      title: string
      description: string
      outline: SynthesisOutlineDraft[]
    },
    context?: RequestContext,
  ): Promise<SynthesisPublishView> {
    return synthesisPublish(await this.request('/syntheses', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        stable_key: input.stableKey,
        domain_key: input.domainKey,
        title: input.title,
        description: input.description,
        outline: synthesisOutlinePayload(input.outline),
      }),
    }, context))
  }

  async publishSynthesis(
    synthesisId: string,
    input: {
      expectedVersionId: string
      title: string
      description: string
      outline: SynthesisOutlineDraft[]
    },
    context?: RequestContext,
  ): Promise<SynthesisPublishView> {
    return synthesisPublish(await this.request(`/syntheses/${encodeURIComponent(synthesisId)}`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        expected_version_id: input.expectedVersionId,
        title: input.title,
        description: input.description,
        outline: synthesisOutlinePayload(input.outline),
      }),
    }, context))
  }

  async mergeConcepts(
    input: {
      sourceConceptId: string
      targetConceptId: string
      expectedSourceVersionId: string
      expectedTargetVersionId: string
      rationale: string
    },
    context?: RequestContext,
  ): Promise<ConceptMergeView> {
    const row = object(await this.request('/concepts/merge', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        source_concept_id: input.sourceConceptId,
        target_concept_id: input.targetConceptId,
        expected_source_version_id: input.expectedSourceVersionId,
        expected_target_version_id: input.expectedTargetVersionId,
        rationale: input.rationale,
      }),
    }, context))
    return {
      status: string(row.status),
      sourceConceptId: string(row.source_concept_id),
      targetConceptId: string(row.target_concept_id),
      targetVersionId: string(row.target_version_id),
      refreshedSynthesisVersions: array(row.refreshed_synthesis_versions).map(string),
    }
  }

  async getConcept(
    conceptId: string,
    context?: RequestContext,
    versionId?: string,
  ): Promise<ConceptView> {
    if (this.apiPrefix === '/internal/next' && context?.sourceVersionId) throw new NoodleApiError('Source scope does not admit multi-source Concepts', 409, '/concepts')
    const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
    const payload = object(await this.request(
      `/concepts/${encodeURIComponent(conceptId)}${query}`,
      { method: 'GET' },
      context,
    ))
    if (this.apiPrefix === '/internal/next') {
      if (context?.sourceVersionId) throw new NoodleApiError('Source scope does not admit multi-source Concepts', 409, '/concepts')
      const revision = object(payload.revision)
      if (versionId && versionId !== revision.id) throw new NoodleApiError('Pinned revision is not current', 409, '/concepts')
      const body = Array.from(string(payload.markdown))
      return { conceptId: string(payload.id), versionId: string(revision.id), title: string(payload.title), originalTitle: string(payload.title), scope: string(revision.scope_json), summary: string(revision.summary), status: 'CURRENT', blocks: array(payload.sections).map(value => { const section = object(value); return { blockKey: string(section.section_id), type: 'MARKDOWN', content: body.slice(number(section.start_offset), number(section.end_offset)).join(''), sourceRole: 'ATOMIC_SUPPORT', sourceRefs: array(section.provenance).map(ref => string(object(ref).source_revision_id)) } }) }
    }
    return {
      conceptId: string(payload.concept_id),
      versionId: string(payload.version_id),
      title: string(payload.title),
      originalTitle: string(payload.original_title),
      scope: string(payload.scope),
      summary: string(payload.summary),
      status: string(payload.status),
      blocks: array(payload.blocks).map((item) => {
        const row = object(item)
        const content = string(row.content)
        return {
          blockKey: string(row.block_key),
          type: string(row.block_type),
          content: content.length > this.config.maxBodyChars
            ? `${content.slice(0, this.config.maxBodyChars)}\n…[truncated]`
            : content,
          sourceRole: string(row.source_role),
          sourceRefs: array(row.evidence).map(item => string(object(item).source_ref)),
        }
      }),
    }
  }

  async acceptAtomicNoteContribution(
    input: {
      conceptId: string
      moduleKey: string
      noteVersionId: string
      expectedConceptVersionId: string
      rationale: string
    },
    context?: RequestContext,
  ): Promise<ConceptContributionUpdateView> {
    const payload = object(await this.request(
      `/concepts/${encodeURIComponent(input.conceptId)}/contributions`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          module_key: input.moduleKey,
          note_version_id: input.noteVersionId,
          expected_concept_version_id: input.expectedConceptVersionId,
          rationale: input.rationale,
        }),
      },
      context,
    ))
    return {
      status: string(payload.status),
      conceptId: string(payload.concept_id),
      conceptVersionId: string(payload.concept_version_id),
      candidateConceptVersionId: string(payload.candidate_concept_version_id),
      contributionId: string(payload.contribution_id),
      updateJobId: string(payload.update_job_id),
      noteVersionId: string(payload.note_version_id),
      moduleKey: string(payload.module_key),
      reused: Boolean(payload.reused),
      synthesisVersions: array(payload.synthesis_versions).map(string),
    }
  }

  async transitionConceptContribution(
    input: {
      contributionId: string
      action: 'REJECT' | 'RETRACT' | 'SUPERSEDE'
      expectedConceptVersionId: string
      rationale: string
      replacementNoteVersionId?: string
    },
    context?: RequestContext,
  ): Promise<ConceptContributionTransitionView> {
    const payload = object(await this.request(
      `/concept-contributions/${encodeURIComponent(input.contributionId)}/transition`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          action: input.action,
          expected_concept_version_id: input.expectedConceptVersionId,
          rationale: input.rationale,
          ...(input.replacementNoteVersionId
            ? { replacement_note_version_id: input.replacementNoteVersionId }
            : {}),
        }),
      },
      context,
    ))
    return {
      status: string(payload.status),
      action: string(payload.action),
      conceptId: string(payload.concept_id),
      contributionId: string(payload.contribution_id),
      previousContributionId: string(payload.previous_contribution_id),
      updateJobId: string(payload.update_job_id),
      moduleKey: string(payload.module_key),
    }
  }

  async suggestConceptCandidates(
    conceptId: string,
    moduleKey: string,
    limit: number,
    context?: RequestContext,
  ): Promise<ConceptCandidateView[]> {
    const params = new URLSearchParams({ module_key: moduleKey, limit: String(limit) })
    const rows = array(await this.request(
      `/concepts/${encodeURIComponent(conceptId)}/candidates?${params}`,
      { method: 'GET' },
      context,
    ))
    return rows.map((item) => {
      const row = object(item)
      return {
        noteVersionId: string(row.note_version_id),
        title: string(row.title),
        thesis: string(row.thesis),
        score: number(row.score),
        pages: array(row.pages).map(number),
        evidenceCount: number(row.evidence_count),
        assetCount: number(row.asset_count),
      }
    })
  }

  async listConceptContributions(
    conceptId: string,
    context?: RequestContext,
  ): Promise<ConceptContributionView[]> {
    const rows = array(await this.request(
      `/concepts/${encodeURIComponent(conceptId)}/contributions`,
      { method: 'GET' },
      context,
    ))
    return rows.map((item) => {
      const row = object(item)
      return {
        contributionId: string(row.contribution_id),
        moduleKey: string(row.module_key),
        noteVersionId: string(row.note_version_id),
        noteTitle: string(row.note_title),
        role: string(row.contribution_role),
        status: string(row.decision_status),
        rationale: string(row.rationale),
      }
    })
  }

  async getConceptUpdateJob(
    jobId: string,
    context?: RequestContext,
  ): Promise<ConceptUpdateJobView> {
    const row = object(await this.request(
      `/concept-updates/${encodeURIComponent(jobId)}`,
      { method: 'GET' },
      context,
    ))
    return {
      jobId: string(row.job_id),
      status: string(row.status),
      operation: string(row.operation),
      conceptId: string(row.concept_id),
      moduleKey: string(row.module_key),
      candidateConceptVersionId: string(row.candidate_concept_version_id),
      errorCode: string(row.error_code),
      errorDetail: string(row.error_detail),
      attemptCount: number(row.attempt_count),
      judges: array(row.judges).map(object),
    }
  }

  private async uploadFile(
    path: string,
    filePath: string,
    fields: Record<string, string>,
    context?: RequestContext,
  ): Promise<JsonObject> {
    const bytes = await readFile(filePath)
    const form = new FormData()
    form.append('file', new Blob([bytes]), basename(filePath))
    for (const [key, value] of Object.entries(fields)) form.append(key, value)
    return object(await this.request(path, { method: 'POST', body: form }, context))
  }

  async ingestPdf(
    filePath: string,
    title: string | undefined,
    context?: RequestContext,
  ): Promise<IngestPdfView> {
    const payload = await this.uploadFile(
      '/sources/pdf',
      filePath,
      title ? { title } : {},
      context,
    )
    return {
      sourceId: string(payload.source_id),
      sourceVersionId: string(payload.source_version_id),
      jobId: string(payload.job_id),
      deduplicated: string(payload.deduplicated),
      jobUrl: string(payload.job_url),
      timelineUrl: string(payload.timeline_url),
      resultUrl: string(payload.result_url),
    }
  }

  async uploadImageAttachment(
    filePath: string,
    analyze: boolean,
    context?: RequestContext,
  ): Promise<AttachmentView> {
    const query = new URLSearchParams({ analyze: String(analyze) })
    const payload = await this.uploadFile(`/attachments/images?${query}`, filePath, {}, context)
    return {
      attachmentId: string(payload.attachment_id),
      originalName: string(payload.original_name),
      status: string(payload.status),
      mediaType: string(payload.media_type),
      byteSize: number(payload.byte_size),
      blobUrl: string(payload.blob_url),
      visionJson: JSON.stringify(object(payload.vision)),
    }
  }

  async restartGeneration(
    jobId: string,
    fromStep: string,
    context?: RequestContext,
  ): Promise<JobStatusView> {
    await this.request(`/jobs/${encodeURIComponent(jobId)}/restart-generation`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from_step: fromStep }),
    }, context)
    return this.getJobStatus(jobId, context)
  }

  async editNoteBlock(
    noteId: string,
    expectedVersionId: string,
    blockKey: string,
    newText: string,
    context?: RequestContext,
  ): Promise<BlockEditView> {
    const payload = object(await this.request(`/notes/${encodeURIComponent(noteId)}/blocks`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_version_id: expectedVersionId,
        block_key: blockKey,
        new_text: newText,
      }),
    }, context))
    return {
      status: string(payload.status),
      noteId: string(payload.note_id),
      previousVersionId: string(payload.previous_version_id),
      versionId: string(payload.version_id),
      versionNo: number(payload.version_no),
      sourceNoteId: string(payload.source_note_id),
      sourceNoteVersionId: string(payload.source_note_version_id),
      blockKey: string(payload.block_key),
      origin: string(payload.origin),
    }
  }

  async replaceSourceImages(
    sourceNoteId: string,
    input: {
      attachmentId: string
      pageStart: number
      pageEnd: number
      expectedSourceNoteVersionId: string
      targetNoteTitle?: string
      caption?: string
      purpose?: string
    },
    context?: RequestContext,
  ): Promise<ImageReplacementView> {
    const payload = object(await this.request(
      `/source-notes/${encodeURIComponent(sourceNoteId)}/replace-images`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          attachment_id: input.attachmentId,
          page_start: input.pageStart,
          page_end: input.pageEnd,
          expected_source_note_version_id: input.expectedSourceNoteVersionId,
          ...(input.targetNoteTitle ? { target_note_title: input.targetNoteTitle } : {}),
          ...(input.caption ? { caption: input.caption } : {}),
          ...(input.purpose ? { purpose: input.purpose } : {}),
        }),
      },
      context,
    ))
    return {
      status: string(payload.status),
      sourceNoteId: string(payload.source_note_id),
      sourceNoteVersionId: string(payload.source_note_version_id),
      previousSourceNoteVersionId: input.expectedSourceNoteVersionId,
      attachmentId: string(payload.attachment_id),
      visualAssetId: string(payload.visual_asset_id),
      removedVisualCount: number(payload.removed_visual_count),
      updatedNotes: array(payload.updated_notes).map((item) => {
        const row = object(item)
        return {
          noteId: string(row.note_id),
          title: string(row.title),
          previousVersionId: string(row.previous_version_id),
          versionId: string(row.version_id),
          versionNo: number(row.version_no),
          containsReplacement: Boolean(row.contains_replacement),
        }
      }),
    }
  }

  async exportMarkdown(
    sourceVersionId: string | undefined,
    context?: RequestContext,
  ): Promise<MarkdownExportView[]> {
    const query = sourceVersionId
      ? `?${new URLSearchParams({ source_version_id: sourceVersionId })}`
      : ''
    const payload = array(await this.request(
      `/admin/export-markdown${query}`,
      { method: 'POST' },
      context,
    ))
    return payload.map((item) => {
      const row = object(item)
      return {
        sourceVersionId: string(row.source_version_id),
        destination: string(row.directory),
        indexPath: string(row.index),
        noteCount: number(row.note_count),
        sourceNotePath: string(row.source_note),
      }
    })
  }
}
