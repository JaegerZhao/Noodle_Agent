import {createServer} from 'node:http'
import {afterEach,beforeEach,describe,expect,it} from 'vitest'
import {Context} from '@deepseek-ai/cordis'
import {CallId} from '@deepseek-ai/dsh-llm'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import {NoodleHttpProvider} from '../src/http-provider.js'
import * as NextTools from '../src/next-tools.js'
import * as ReadTools from '../src/read-tools.js'
let ctx:Context,server:ReturnType<typeof createServer>,requests:string[]
beforeEach(async()=>{
 requests=[];server=createServer((req,res)=>{requests.push(req.url||'');res.setHeader('Content-Type','application/json');res.end(JSON.stringify(req.url?.endsWith('/answer')?{request_id:'request-test',status:'PROFILE_MISMATCH',message:'profile missing',answer_markdown:'',citations:[]}:{status:'READY',items:[],total:0}))})
 await new Promise<void>(r=>server.listen(0,'127.0.0.1',r));const a=server.address();if(!a||typeof a==='string')throw Error('no server')
 ctx=new Context();await ctx.plugin(SystemPrompt);await ctx.plugin(ToolRuntime);await ctx.plugin(NoodleHttpProvider,{baseUrl:'http://127.0.0.1:'+a.port,timeoutMs:3000,maxBodyChars:1000})
 await ctx.plugin(NextTools);await ctx.plugin(ReadTools)
})
afterEach(async()=>{await ctx.fiber.dispose();await new Promise<void>(r=>server.close(()=>r()))})
const input=(name:string,args={})=>({signal:new AbortController().signal,callId:CallId('test-call'),name,arguments:args})
describe('typed Noodle tools',()=>{
 it('uses compact paginated library via the shared provider',async()=>{const r=await ctx.tools.execute(input('noodle_list_knowledge',{kind:'MOC',query:'地图',offset:20}));expect(r.isError).toBe(false);const q=new URL(requests[0]!,'http://local').searchParams;expect(q.get('limit')).toBe('20');expect(q.get('offset')).toBe('20');expect(requests[0]).toContain('/agent/library?')})
 it('requires approval before an edit reaches HTTP',async()=>{const r=await ctx.tools.execute(input('noodle_edit_section',{note_id:'n',expected_revision:'r',section_id:'main',markdown:'修订'}));expect(r.isError).toBe(true);expect(requests).toEqual([])})
 it('dispatches an approved write once and preserves cancellation rejection',async()=>{
  let outcome='allowed-once',prompts=0
  ctx.provide('approval');ctx.set('approval',{request:async()=>{prompts++;return outcome}} as any)
  const agent={session:{id:'isolated-test',events:[]},ctx} as any
  const args=input('noodle_edit_section',{note_id:'n',expected_revision:'r',section_id:'main',markdown:'修订'})
  const approved=await ctx.tools.execute({...args,agent});expect(approved.isError).toBe(false);expect(requests).toHaveLength(1)
  outcome='cancelled';const cancelled=await ctx.tools.execute({...args,agent,callId:CallId('cancelled')});expect(cancelled.isError).toBe(true);expect(requests).toHaveLength(1);expect(prompts).toBe(2)
 })
 it('concludes an ordinary answer turn on business failure without requiring another LLM step',async()=>{const r=await ctx.tools.execute(input('noodle_answer_knowledge',{question:'梯度'}));expect(r.isError).toBe(false);expect(r.concludesTurn).toBe(true);expect(r.value).toMatchObject({status:'PROFILE_MISMATCH'});expect(requests).toHaveLength(1)})
 it('discovers sources and tasks, without registering legacy/filesystem operations',()=>{const names=ctx.tools.schemas().map(t=>t.name);expect(names).toHaveLength(17);expect(names).toEqual(expect.arrayContaining(['noodle_list_sources','noodle_list_tasks','noodle_get_readiness','noodle_pause_task','noodle_resume_task']));expect(names.some(n=>/synthesis|filesystem|sql/.test(n))).toBe(false)})
})
