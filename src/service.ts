import { Service, type Context } from '@deepseek-ai/cordis'
import type {
  AttachmentView,
  AnswerResultView,
  AtomicNoteView,
  BlockEditView,
  ImageReplacementView,
  IngestPdfView,
  JobStatusView,
  MarkdownExportView,
  RequestContext,
  SearchResultView,
  SourceNoteListView,
  SourceNoteView,
  SynthesisListView,
  SynthesisView,
  ConceptView,
  ConceptContributionUpdateView,
  ConceptContributionTransitionView,
  ConceptCandidateView,
  ConceptContributionView,
  ConceptUpdateJobView,
  ConceptRegistryView,
  SynthesisOutlineDraft,
  SynthesisPublishView,
  ConceptMergeView,
} from './contracts.js'

declare module '@deepseek-ai/cordis' {
  interface Context {
    noodle: NoodleService
  }
}

/** Stable capability seam consumed by model-facing Noodle tools. */
export abstract class NoodleService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'noodle')
  }

  abstract searchKnowledge(query: string, limit: number, context?: RequestContext): Promise<SearchResultView>
  abstract answerKnowledge(question: string, limit: number, context?: RequestContext): Promise<AnswerResultView>
  abstract listSourceNotes(limit: number, context?: RequestContext): Promise<SourceNoteListView>
  abstract getSourceNote(sourceNoteId: string, context?: RequestContext): Promise<SourceNoteView>
  abstract getJobStatus(jobId: string, context?: RequestContext): Promise<JobStatusView>
  abstract getNote(noteId: string, context?: RequestContext): Promise<AtomicNoteView>
  abstract listSyntheses(context?: RequestContext): Promise<SynthesisListView>
  abstract getSynthesis(synthesisId: string, context?: RequestContext): Promise<SynthesisView>
  abstract getConcept(
    conceptId: string,
    context?: RequestContext,
    versionId?: string,
  ): Promise<ConceptView>
  abstract listConcepts(query: string, limit: number, context?: RequestContext): Promise<ConceptRegistryView>
  abstract createSynthesis(
    input: {
      stableKey: string
      domainKey: string
      title: string
      description: string
      outline: SynthesisOutlineDraft[]
    },
    context?: RequestContext,
  ): Promise<SynthesisPublishView>
  abstract publishSynthesis(
    synthesisId: string,
    input: {
      expectedVersionId: string
      title: string
      description: string
      outline: SynthesisOutlineDraft[]
    },
    context?: RequestContext,
  ): Promise<SynthesisPublishView>
  abstract mergeConcepts(
    input: {
      sourceConceptId: string
      targetConceptId: string
      expectedSourceVersionId: string
      expectedTargetVersionId: string
      rationale: string
    },
    context?: RequestContext,
  ): Promise<ConceptMergeView>
  abstract acceptAtomicNoteContribution(
    input: {
      conceptId: string
      moduleKey: string
      noteVersionId: string
      expectedConceptVersionId: string
      rationale: string
    },
    context?: RequestContext,
  ): Promise<ConceptContributionUpdateView>
  abstract transitionConceptContribution(
    input: {
      contributionId: string
      action: 'REJECT' | 'RETRACT' | 'SUPERSEDE'
      expectedConceptVersionId: string
      rationale: string
      replacementNoteVersionId?: string
    },
    context?: RequestContext,
  ): Promise<ConceptContributionTransitionView>
  abstract suggestConceptCandidates(
    conceptId: string,
    moduleKey: string,
    limit: number,
    context?: RequestContext,
  ): Promise<ConceptCandidateView[]>
  abstract listConceptContributions(
    conceptId: string,
    context?: RequestContext,
  ): Promise<ConceptContributionView[]>
  abstract getConceptUpdateJob(
    jobId: string,
    context?: RequestContext,
  ): Promise<ConceptUpdateJobView>
  abstract ingestPdf(
    filePath: string,
    title: string | undefined,
    context?: RequestContext,
  ): Promise<IngestPdfView>
  abstract uploadImageAttachment(
    filePath: string,
    analyze: boolean,
    context?: RequestContext,
  ): Promise<AttachmentView>
  abstract restartGeneration(
    jobId: string,
    fromStep: string,
    context?: RequestContext,
  ): Promise<JobStatusView>
  abstract editNoteBlock(
    noteId: string,
    expectedVersionId: string,
    blockKey: string,
    newText: string,
    context?: RequestContext,
  ): Promise<BlockEditView>
  abstract replaceSourceImages(
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
  ): Promise<ImageReplacementView>
  abstract exportMarkdown(
    sourceVersionId: string | undefined,
    context?: RequestContext,
  ): Promise<MarkdownExportView[]>
}
