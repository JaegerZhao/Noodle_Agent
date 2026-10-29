import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type {} from './service.js'
import {domainOutput,executeDomain,requestContext} from './tool-contract.js'
export const name='noodle-read-tools'
export const inject=['tools','noodle']
const queryParameters={
  source_version_id:{type:'string' as const,description:'Optional current source revision identity; never a note ID.'},
  scope_mode:{type:'string' as const,enum:['GLOBAL','SOURCE','NOTES']},
  source_id:{type:'string' as const},note_ids:{type:'array' as const,items:{type:'string' as const}},
  retrieval_mode:{type:'string' as const,enum:['HYBRID','KEYWORD_ONLY']},
  entity_note_ids:{type:'array' as const,items:{type:'string' as const},description:'Pronoun hints, not a strict scope.'},
  limit:{type:'integer' as const,description:'Maximum final evidence hits, 1–20; default 6.'},
}
function queryContext(args:any,exec:any){
  const scope=args.scope_mode?{mode:args.scope_mode,...(args.scope_mode==='SOURCE'?{source_ids:[args.source_id]}:{}),...(args.scope_mode==='NOTES'?{note_ids:args.note_ids||[]}:{})}:undefined
  return {...requestContext(exec),...(scope?{scope}:{}),...(args.source_version_id?{sourceVersionId:args.source_version_id}:{}),...(args.retrieval_mode?{retrievalMode:args.retrieval_mode}:{}),...(args.entity_note_ids?{entityNoteIds:args.entity_note_ids}:{})}
}
export function apply(ctx:Context):void{
  ctx.tools.register(defineTool({
    name:'noodle_answer_knowledge',
    description:'Answer a knowledge question once using current Atomic and Concept evidence. This tool already searches and checks support. Present answer_markdown and its exact citations without rewriting facts. Non-READY status is a business failure, not an answer; report it and stop, never enumerate the library as fallback.',
    parameters:{question:{type:'string',required:true},continue_task:{type:'boolean',description:'Only true for an explicit multi-step user request with further actions after this answer. Ordinary questions omit this.'},...queryParameters},output:domainOutput,timeoutMs:200_000,isConcurrencySafe:()=>false,
    async execute(args,exec){
      if(!args.question.trim())throw Error('question must not be blank')
      const value=await executeDomain(exec,async()=>{try{return await ctx.noodle.answerKnowledge(args.question.trim(),Math.min(20,Math.max(1,args.limit??6)),queryContext(args,exec))}catch(error){if(exec.signal.aborted)throw error;return {status:'PROVIDER_ERROR',answer_markdown:'',citations:[],message:'知识回答请求失败：'+(error instanceof Error?error.message:String(error))}}})
      if(!args.continue_task||value.status!=='READY')exec.concludeTurn()
      return value
    }
  }))
  ctx.tools.register(defineTool({
    name:'noodle_search_knowledge',description:'Locate relevant current notes. Not a mandatory step before answer_knowledge. Scope is explicit; SOURCE only admits Atomic. Citation IDs identify exact note revisions and source pages.',
    parameters:{query:{type:'string',required:true},...queryParameters},output:domainOutput,isConcurrencySafe:()=>true,
    async execute(args,exec){if(!args.query.trim())throw Error('query must not be blank');return executeDomain(exec,()=>ctx.noodle.searchKnowledge(args.query.trim(),Math.min(20,Math.max(1,args.limit??6)),queryContext(args,exec)))}
  }))
  ctx.tools.register(defineTool({
    name:'noodle_get_atomic_note',description:'Read an Atomic by note_id, optionally one section. Omit section_id for its compact outline; read the target section before editing. source_revision_id is a different identity.',
    parameters:{note_id:{type:'string',required:true},section_id:{type:'string'}},output:domainOutput,isConcurrencySafe:()=>true,
    execute(args,exec){return executeDomain(exec,()=>ctx.noodle.requestDomain('/agent/notes/'+encodeURIComponent(args.note_id)+(args.section_id?'?section_id='+encodeURIComponent(args.section_id):''),requestContext(exec)))}
  }))
  ctx.tools.register(defineTool({
    name:'noodle_list_concepts',description:'Find current Concept identities by title or alias. Returns a compact registry, not full content. Prefer a specific query.',
    parameters:{query:{type:'string'},limit:{type:'integer'}},output:domainOutput,isConcurrencySafe:()=>true,
    execute(args,exec){return executeDomain(exec,()=>ctx.noodle.listConcepts(args.query||'',Math.min(30,Math.max(1,args.limit??10)),requestContext(exec)))}
  }))
  ctx.tools.register(defineTool({
    name:'noodle_get_concept',description:'Read a current Concept outline or one section. source_revisions are provenance, not Atomic note IDs. Read supporting_notes to locate the actual Atomic.',
    parameters:{concept_id:{type:'string',required:true},section_id:{type:'string'}},output:domainOutput,isConcurrencySafe:()=>true,
    execute(args,exec){return executeDomain(exec,()=>ctx.noodle.requestDomain('/agent/notes/'+encodeURIComponent(args.concept_id)+(args.section_id?'?section_id='+encodeURIComponent(args.section_id):''),requestContext(exec)))}
  }))
}
