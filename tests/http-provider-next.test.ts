import {createServer} from 'node:http'
import {afterEach,beforeEach,expect,it} from 'vitest'
import {Context} from '@deepseek-ai/cordis'
import {NoodleHttpProvider} from '../src/http-provider.js'
let server:ReturnType<typeof createServer>,ctx:Context,requests:any[],stale=false
const citation={citation_id:'K1',note_id:'c1',revision_id:'r1',section_id:'main',kind:'CONCEPT',title:'梯度',start_offset:1,end_offset:8,sha256:'a'.repeat(64),evidence_ids:['e1'],provenance:[{source_revision_id:'sr1',source_sha256:'b'.repeat(64),page:9}]}
beforeEach(async()=>{
 requests=[];stale=false
 server=createServer(async(req,res)=>{
  let text='';for await(const part of req)text+=part
  requests.push({path:req.url,body:text?JSON.parse(text):{},headers:req.headers});res.setHeader('Content-Type','application/json')
  if(req.url?.includes('/source-revisions/')){res.statusCode=stale?409:200;res.end(JSON.stringify(stale?{detail:'not current'}:{source_id:'s1'}));return}
  if(req.url?.endsWith('/search')){res.end(JSON.stringify({status:'READY',request_id:'request-1',counts:{atomic_notes:0,concept_notes:1},hits:[{...citation,body:'知识正文',score:0.5}]}));return}
  if(req.url?.endsWith('/answer')){res.end(JSON.stringify({status:stale?'PROFILE_MISMATCH':'READY',message:stale?'missing profile':'',request_id:'request-1',answer_markdown:stale?'':'解释 [K1]',citations:stale?[]:[citation],hits:[]}));return}
  if(req.url?.includes('/agent/notes/')){res.end(JSON.stringify({note_id:req.url.endsWith('c1')?'c1':'n1',kind:req.url.endsWith('c1')?'CONCEPT':'ATOMIC',revision_id:'r1',sections:[{section_id:'s',provenance:[{source_revision_id:'sr1',page:2}]}]}));return}
  res.statusCode=404;res.end('{}')
 })
 await new Promise<void>(r=>server.listen(0,'127.0.0.1',r));const a=server.address();if(!a||typeof a==='string')throw Error('no server')
 ctx=new Context();await ctx.plugin(NoodleHttpProvider,{baseUrl:'http://127.0.0.1:'+a.port,apiPrefix:'/internal/next',retrievalMode:'KEYWORD_ONLY',timeoutMs:2000,maxBodyChars:1000})
})
afterEach(async()=>{await ctx.fiber.dispose();await new Promise<void>(r=>server.close(()=>r()))})
it('preserves the complete citation and counts without invented quality/score fields',async()=>{
 const result=await ctx.noodle.searchKnowledge('梯度',5)
 expect(result).toMatchObject({status:'READY',request_id:'request-1',counts:{atomic_notes:0,concept_notes:1}})
 expect(result.hits[0]).toMatchObject(citation)
 const answer=await ctx.noodle.answerKnowledge('梯度',5)
 const {evidence_ids,...locator}=citation;expect(answer.citations[0]).toEqual(locator);expect(answer.citations[0]).not.toHaveProperty('score');expect(answer.hits).toEqual([])
})
it('resolves selected source and forwards identity plus trace headers',async()=>{
 await ctx.noodle.searchKnowledge('它是什么',5,{sourceVersionId:'sr1',entityNoteIds:['n1'],sessionId:'s',callId:'call'})
 expect(requests[1].body).toMatchObject({scope:{mode:'SOURCE',source_ids:['s1']},allowed_kinds:['ATOMIC'],entity_note_ids:['n1'],retrieval_mode:'KEYWORD_ONLY'})
 expect(requests[1].headers['x-harness-session-id']).toBe('s');expect(requests[1].headers['x-harness-tool-call-id']).toBe('call')
})
it('fails closed for a stale selected source',async()=>{stale=true;await expect(ctx.noodle.searchKnowledge('梯度',5,{sourceVersionId:'sr1'})).rejects.toMatchObject({status:409});expect(requests).toHaveLength(1)})
it('preserves business failure even over HTTP 200',async()=>{stale=true;expect(await ctx.noodle.answerKnowledge('梯度',6)).toMatchObject({status:'PROFILE_MISMATCH',message:'missing profile',answer_markdown:'',citations:[]})})
it('supports strict NOTES and per-call retrieval mode',async()=>{await ctx.noodle.searchKnowledge('梯度',6,{scope:{mode:'NOTES',note_ids:['n1']},retrievalMode:'HYBRID'});expect(requests[0].body).toMatchObject({scope:{mode:'NOTES',note_ids:['n1']},retrieval_mode:'HYBRID'})})
it('uses current compact note DTO without synthetic review or asset values',async()=>{const n=await ctx.noodle.getNote('n1');expect(n).toHaveProperty('revision_id','r1');expect(n).not.toHaveProperty('quality');expect(n).not.toHaveProperty('blocks')})
it('rejects historical Concept pins',async()=>{await expect(ctx.noodle.getConcept('c1',undefined,'old')).rejects.toMatchObject({status:409})})
it('preserves user cancellation as AbortError instead of a network failure with an object message',async()=>{const controller=new AbortController();controller.abort({kind:'user-cancel'});await expect(ctx.noodle.searchKnowledge('取消',5,{signal:controller.signal})).rejects.toMatchObject({name:'AbortError',message:'操作已停止'})})
