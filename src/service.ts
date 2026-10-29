import { Service, type Context } from '@deepseek-ai/cordis'
import type { RequestContext, SearchResultView, AnswerResultView, AtomicNoteView, ConceptView, ConceptRegistryView } from './contracts.js'

declare module '@deepseek-ai/cordis' {
    interface Context {
        noodle: NoodleService;
    }
}
/** Stable capability seam consumed by model-facing Noodle tools. */
export abstract class NoodleService extends Service {
    abstract requestDomain(path: string, context?: RequestContext, body?: unknown, method?: string): Promise<Record<string, unknown>>;
    constructor(ctx: Context) {
        super(ctx, 'noodle');
    }
    abstract searchKnowledge(query: string, limit: number, context?: RequestContext): Promise<SearchResultView>;
    abstract answerKnowledge(question: string, limit: number, context?: RequestContext): Promise<AnswerResultView>;
    abstract getNote(noteId: string, context?: RequestContext): Promise<AtomicNoteView>;
    abstract getConcept(conceptId: string, context?: RequestContext, versionId?: string): Promise<ConceptView>;
    abstract listConcepts(query: string, limit: number, context?: RequestContext): Promise<ConceptRegistryView>;
}
