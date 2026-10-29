import type { Context } from '@deepseek-ai/cordis'
import type { RequestContext, SearchResultView, AnswerResultView, AtomicNoteView, ConceptView, ConceptRegistryView, JsonObject } from './contracts.js'
import { NoodleService } from './service.js'
export interface HttpProviderConfig {
  baseUrl: string; apiPrefix?: string; retrievalMode?: 'HYBRID' | 'KEYWORD_ONLY'; timeoutMs: number; maxBodyChars: number;
}
export class NoodleApiError extends Error {
  constructor(message: string, readonly status: number, readonly path: string, readonly detail = '') { super(message); this.name = 'NoodleApiError' }
}
/** One HTTP boundary for reads and approval-gated writes. Preserve domain DTOs. */
export class NoodleHttpProvider extends NoodleService {
  private readonly baseUrl: string
  constructor(ctx: Context, private readonly config: HttpProviderConfig) {
    super(ctx); this.baseUrl = config.baseUrl.replace(/\/+$/, '')
    if ((config.apiPrefix || '/internal/next') !== '/internal/next') throw Error('Only /internal/next is supported')
  }
  async requestDomain(path: string, context?: RequestContext, body?: unknown, method?: string): Promise<JsonObject> {
    if (!path.startsWith('/') || path.startsWith('//') || path.includes('..')) throw Error('Invalid domain path')
    const headers = new Headers({Accept:'application/json'})
    if (body !== undefined) headers.set('Content-Type','application/json')
    if (context?.callId) headers.set('X-Harness-Tool-Call-Id', context.callId)
    if (context?.sessionId) headers.set('X-Harness-Session-Id', context.sessionId)
    if (context?.userRpcId) headers.set('X-Harness-User-Rpc-Id', context.userRpcId)
    // A complete RAG request includes recall, answer and one support check.
    // Legacy short metadata timeouts must not cancel a valid answer midway.
    const deadline=AbortSignal.timeout(path==='/answer'?Math.max(180_000,this.config.timeoutMs):this.config.timeoutMs)
    const signal = context?.signal ? AbortSignal.any([context.signal, deadline]) : deadline
    let response: Response
    try {
      response = await fetch(this.baseUrl+'/internal/next'+path, {method: method || (body === undefined ? 'GET' : 'POST'), headers, signal, ...(body === undefined ? {} : {body:JSON.stringify(body)})})
    } catch (error) {
      if(context?.signal?.aborted)throw new DOMException('操作已停止','AbortError')
      throw new NoodleApiError('Noodle API request failed: '+(error instanceof Error ? error.message : String(error)),0,path)
    }
    const payload = await response.json() as JsonObject
    if (!response.ok) throw new NoodleApiError('Noodle '+response.status+': '+String(payload.detail || payload.message || payload.status || 'Request failed'),response.status,path)
    return {...payload, workbench_url:this.baseUrl+'/app/'}
  }
  private async query(query: string, limit: number, context?: RequestContext) {
    let scope = context?.scope || {mode:'GLOBAL' as const}
    if (context?.sourceVersionId) {
      const source = await this.requestDomain('/source-revisions/'+encodeURIComponent(context.sourceVersionId),context)
      if (!source.source_id) throw Error('Selected source revision could not be resolved')
      scope = {mode:'SOURCE', source_ids:[String(source.source_id)]}
    }
    return {query, top_k:limit, scope, allowed_kinds: scope.mode === 'SOURCE' ? ['ATOMIC'] : ['ATOMIC','CONCEPT'], retrieval_mode:context?.retrievalMode || this.config.retrievalMode || 'HYBRID', rerank:'AUTO', entity_note_ids:context?.entityNoteIds || []}
  }
  async searchKnowledge(query: string, limit: number, context?: RequestContext): Promise<SearchResultView> {
    return await this.requestDomain('/search',context,await this.query(query,limit,context)) as SearchResultView
  }
  async answerKnowledge(question: string, limit: number, context?: RequestContext): Promise<AnswerResultView> {
    const value=await this.requestDomain('/answer',context,await this.query(question,limit,context)) as AnswerResultView
    // Full recall passages and hundreds of evidence IDs belong in the immutable
    // answer receipt, not in a second model context or a spill-prone UI string.
    const citations=(value.citations||[]).map(c=>({citation_id:c.citation_id,note_id:c.note_id,revision_id:c.revision_id,
      section_id:c.section_id,kind:c.kind,title:c.title,start_offset:c.start_offset,end_offset:c.end_offset,sha256:c.sha256,
      provenance:c.provenance,...(c.chunk_id?{chunk_id:c.chunk_id}:{}),...(c.matched_ranges?{matched_ranges:c.matched_ranges}:{})}))
    return {status:value.status,request_id:value.request_id,message:value.message,answer_markdown:value.answer_markdown,citations,hits:[],
      ...Object.fromEntries(['counts','snapshot','timings_ms','degraded_reason','workbench_url','source_titles','usage_summary'].filter(k=>k in value).map(k=>[k,value[k]]))}
  }
  async getNote(noteId: string, context?: RequestContext): Promise<AtomicNoteView> {
    return this.requestDomain('/agent/notes/'+encodeURIComponent(noteId),context)
  }
  async listConcepts(query: string, limit: number, context?: RequestContext): Promise<ConceptRegistryView> {
    return this.requestDomain('/concepts?'+new URLSearchParams({q:query,limit:String(limit)}),context)
  }
  async getConcept(conceptId: string, context?: RequestContext, versionId?: string): Promise<ConceptView> {
    const value = await this.requestDomain('/agent/notes/'+encodeURIComponent(conceptId),context)
    if (value.kind !== 'CONCEPT') throw Error('Expected a Concept note_id')
    if (versionId && value.revision_id !== versionId) throw new NoodleApiError('Pinned revision is not current',409,'/concepts')
    return value
  }
}
