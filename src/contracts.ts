export interface RequestContext {
  sourceVersionId?: string
  signal?: AbortSignal
  callId?: string
  sessionId?: string
}

export interface EvidenceRef {
  pageLabel: string
  quote: string
  level: string
}

export interface SearchHitView {
  entityType: 'ATOMIC_NOTE' | 'CONCEPT'
  noteId: string
  noteVersionId: string
  conceptId: string
  synthesisPaths: string[]
  title: string
  thesis: string
  body: string
  score: number
  evidence: EvidenceRef[]
  assetCount: number
}

export interface SearchResultView {
  query: string
  count: number
  hits: SearchHitView[]
}

export interface CitationView {
  noteId: string
  noteVersionId: string
  title: string
  score: number
  pages: string[]
}

export interface AnswerResultView {
  question: string
  answer: string
  coverageWarning: string
  citations: CitationView[]
}

export interface SourceNoteListItem {
  sourceNoteId: string
  versionId: string
  sourceId: string
  sourceVersionId: string
  title: string
  summary: string
  versionNo: number
}

export interface SourceNoteListView {
  count: number
  notes: SourceNoteListItem[]
}

export interface AtomicNoteRef {
  noteId: string
  versionId: string
  title: string
  thesis: string
  pages: string[]
}

export interface OutlineEntryView {
  stableKey: string
  title: string
  level: number
  noteCount: number
  notes: AtomicNoteRef[]
}

export interface SourceNoteView {
  sourceNoteId: string
  versionId: string
  sourceVersionId: string
  sourceFile: string
  title: string
  summary: string
  versionNo: number
  unitCount: number
  outline: OutlineEntryView[]
}

export interface JobStatusView {
  jobId: string
  sourceVersionId: string
  status: string
  stage: string
  progress: number
  warnings: string[]
  errorCode: string
  errorDetail: string
  updatedAt: string
  harnessSessionId: string
  harnessToolCallId: string
}

export interface IngestPdfView {
  sourceId: string
  sourceVersionId: string
  jobId: string
  deduplicated: string
  jobUrl: string
  timelineUrl: string
  resultUrl: string
}

export interface AttachmentView {
  attachmentId: string
  originalName: string
  status: string
  mediaType: string
  byteSize: number
  blobUrl: string
  visionJson: string
}

export interface NoteBlockView {
  assetPages?: string[]
  evidence?: EvidenceRef[]
  blockKey: string
  type: string
  text: string
  origin: string
  assetCount: number
}

export interface AtomicNoteView {
  noteId: string
  versionId: string
  title: string
  thesis: string
  quality: string
  review: string
  blocks: NoteBlockView[]
}

export interface BlockEditView {
  status: string
  noteId: string
  previousVersionId: string
  versionId: string
  versionNo: number
  sourceNoteId: string
  sourceNoteVersionId: string
  blockKey: string
  origin: string
}

export interface UpdatedNoteView {
  noteId: string
  title: string
  previousVersionId: string
  versionId: string
  versionNo: number
  containsReplacement: boolean
}

export interface ImageReplacementView {
  status: string
  sourceNoteId: string
  sourceNoteVersionId: string
  previousSourceNoteVersionId: string
  attachmentId: string
  visualAssetId: string
  removedVisualCount: number
  updatedNotes: UpdatedNoteView[]
}

export interface MarkdownExportView {
  sourceVersionId: string
  destination: string
  indexPath: string
  noteCount: number
  sourceNotePath: string
}

export interface SynthesisListItemView {
  synthesisId: string
  versionId: string
  title: string
  description: string
  status: string
  conceptCount: number
  coverage: number
}

export interface SynthesisListView {
  count: number
  syntheses: SynthesisListItemView[]
}

export interface SynthesisOutlineEntryView {
  nodeId: string
  stableKey: string
  title: string
  originalTitle: string
  level: number
  nodeKind: string
  conceptId: string
  conceptVersionId: string
  pages: number[]
}

export interface SynthesisView {
  synthesisId: string
  versionId: string
  title: string
  description: string
  status: string
  outline: SynthesisOutlineEntryView[]
}

export interface ConceptRegistryItemView {
  conceptId: string
  stableKey: string
  versionId: string
  versionNo: number
  title: string
  summary: string
  aliases: string[]
  synthesisPaths: string[]
  synthesisCount: number
}

export interface ConceptRegistryView {
  count: number
  concepts: ConceptRegistryItemView[]
}

export interface SynthesisOutlineDraft {
  stableKey?: string
  title: string
  nodeKind: 'GROUP' | 'CONCEPT'
  conceptId?: string
  children: SynthesisOutlineDraft[]
}

export interface SynthesisPublishView {
  status: string
  synthesisId: string
  versionId: string
  versionNo: number
  conceptCount: number
}

export interface ConceptMergeView {
  status: string
  sourceConceptId: string
  targetConceptId: string
  targetVersionId: string
  refreshedSynthesisVersions: string[]
}

export interface ConceptBlockView {
  blockKey: string
  type: string
  content: string
  sourceRole: string
  sourceRefs: string[]
}

export interface ConceptView {
  conceptId: string
  versionId: string
  title: string
  originalTitle: string
  scope: string
  summary: string
  status: string
  blocks: ConceptBlockView[]
}

export interface ConceptContributionUpdateView {
  status: string
  conceptId: string
  conceptVersionId: string
  candidateConceptVersionId: string
  contributionId: string
  updateJobId: string
  noteVersionId: string
  moduleKey: string
  reused: boolean
  synthesisVersions: string[]
}

export interface ConceptContributionTransitionView {
  status: string
  action: string
  conceptId: string
  contributionId: string
  previousContributionId: string
  updateJobId: string
  moduleKey: string
}

export interface ConceptCandidateView {
  noteVersionId: string
  title: string
  thesis: string
  score: number
  pages: number[]
  evidenceCount: number
  assetCount: number
}

export interface ConceptUpdateJobView {
  jobId: string
  status: string
  operation: string
  conceptId: string
  moduleKey: string
  candidateConceptVersionId: string
  errorCode: string
  errorDetail: string
  attemptCount: number
  judges: Record<string, any>[]
}

export interface ConceptContributionView {
  contributionId: string
  moduleKey: string
  noteVersionId: string
  noteTitle: string
  role: string
  status: string
  rationale: string
}
