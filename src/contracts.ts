/** Domain identities stay distinct through the Harness boundary. */
export type JsonObject = Record<string, unknown>
export interface RetrievalScope { mode: 'GLOBAL' | 'SOURCE' | 'NOTES'; source_ids?: string[]; note_ids?: string[] }
export interface RequestContext {
  sourceVersionId?: string; scope?: RetrievalScope; retrievalMode?: 'HYBRID' | 'KEYWORD_ONLY';
  entityNoteIds?: string[]; signal?: AbortSignal; callId?: string; sessionId?: string; userRpcId?: string;
}
export interface CitationView extends JsonObject {
  citation_id: string; note_id: string; revision_id: string; section_id: string;
  kind: 'ATOMIC' | 'CONCEPT'; title: string; start_offset: number; end_offset: number;
  sha256: string; provenance: {source_revision_id: string; source_sha256: string; page: number | null}[];
}
export interface SearchResultView extends JsonObject {
  status: string; request_id: string; message: string;
  hits: (CitationView & {body: string; score: number})[];
}
export interface AnswerResultView extends SearchResultView { answer_markdown: string; citations: CitationView[] }
export type AtomicNoteView = JsonObject
export type ConceptView = JsonObject
export type ConceptRegistryView = JsonObject
