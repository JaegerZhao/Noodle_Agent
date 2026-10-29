import {afterEach,describe,it,expect,vi} from 'vitest'
import {AgentClient,foldEvents,mergeSessionEvents,stripScope,parseToolJson,citationSources,formatRagUsage,ApprovalInbox} from '../services/noodle/src/noodle_agent/web/next/agent-client.js'
const event=(seq:number,type:string,data:any,time=seq*100)=>({event:{seq,type,data,time}})
afterEach(()=>vi.useRealTimers())
describe('DSH client consumes authoritative history',()=>{
 it('shows real text chunks before turn end and replaces them with the committed message without duplication',()=>{
  const entries=[event(1,'turn/start',{turn:1}),event(2,'assistant/chunk',{step:0,chunk:{type:'text-delta',index:0,text:'注意力'}}),event(3,'assistant/chunk',{step:0,chunk:{type:'reasoning-delta',index:1,text:'private reasoning'}}),event(4,'assistant/chunk',{step:0,chunk:{type:'text-delta',index:0,text:'机制'}})]
  expect(foldEvents(entries)[0].streamingText).toBe('注意力机制');expect(foldEvents(entries)[0].running).toBe(true)
  const committed=foldEvents([...entries,event(5,'assistant/message',{step:0,message:{content:[{type:'text',text:'注意力机制。'}]}})])[0]
  expect(committed.streamingText).toBe('');expect(committed.texts).toEqual(['注意力机制。'])
 })
 it('merges recovered history with live and explicitly loaded older events, without repeats or lost deltas',()=>{
  const early=event(1,'turn/start',{turn:1}),chunk=event(3,'assistant/chunk',{step:0,chunk:{type:'text-delta',index:0,text:'已输出'}}),end=event(4,'turn/end',{})
  const result=mergeSessionEvents([early,chunk],[chunk,end]);expect(result.map(x=>x.event.seq)).toEqual([1,3,4]);expect(foldEvents(result)[0].running).toBe(false)
 })
 it('closes an interrupted turn from authoritative idle status without fabricating a completed answer',()=>{
  const entries=[event(1,'turn/start',{turn:1}),event(2,'tool/call',{callId:'c',name:'noodle_answer_knowledge'}),event(3,'tool/result',{message:{content:[{type:'tool-result',toolCallId:'c',isError:true,content:[{type:'text',text:'AbortError'}]}]}})];
  expect(foldEvents(entries)[0].running).toBe(true);
  const [turn]=foldEvents(entries,{running:false});expect(turn.running).toBe(false);expect(turn.interrupted).toBe(true);expect(turn.answers).toEqual([]);expect(turn.elapsedMs).toBeUndefined();expect(turn.tools[0].error).toBe(true)
  const resumed=foldEvents([...entries,event(4,'turn/start',{turn:2})],{running:true});expect(resumed[0].running).toBe(false);expect(resumed[1].running).toBe(true)
 })
 it('reports approval waiting separately from total elapsed time without double counting replay',()=>{
  const asked=event(2,'approval/asked',{id:'a'},200);
  const [turn]=foldEvents([event(1,'turn/start',{turn:1},100),asked,asked,event(3,'approval/decided',{id:'a',outcome:'rejected'},800),event(4,'turn/end',{},1100)]);
  expect(turn.approvalWaitMs).toBe(600);expect(turn.elapsedMs).toBe(1000)
 })
 it('keeps pending approval across history rendering, session switches and replay until resolved',()=>{
  const inbox=new ApprovalInbox(),frame={type:'approval/requested',approvalId:'a',rpcId:'wire-id',sessionId:'s1'};
  inbox.apply(frame);inbox.apply(frame);expect(inbox.forSession('s1')).toHaveLength(1);expect(inbox.forSession('s2')).toEqual([]);
  foldEvents([event(1,'turn/start',{turn:1}),event(2,'tool/call',{name:'noodle_edit_section',callId:'call'})]);
  expect(inbox.forSession('s1')[0].rpcId).toBe('wire-id');inbox.apply({type:'approval/resolved',approvalId:'a'});expect(inbox.forSession('s1')).toEqual([])
 })
 it('preserves a failed approval response as an error rather than pretending approval completed',async()=>{
  const client=new AgentClient({fetcher:async()=>({ok:false,json:async()=>({detail:'not pending'})})});
  await expect(client.respond({rpcId:'a',approvalId:'b',sessionId:'s'},'rejected')).rejects.toThrow('not pending')
 })
 it('shows only used RAG supplier groups and never fabricates missing token usage',()=>{
  expect(formatRagUsage({llm:{calls:2,total_tokens:1234},embedding:{calls:1,total_tokens:20},rerank:{calls:1}})).toBe('RAG 文本 2 次 · 1,234 Token；嵌入 1 次 · 20 Token；重排 1 次')
  expect(formatRagUsage({})).toBe('')
 })
 it('groups pages by source, sorts and compacts ranges without mixing PDFs',()=>{expect(citationSources({provenance:[{source_revision_id:'a',page:3},{source_revision_id:'b',page:1},{source_revision_id:'a',page:2},{source_revision_id:'a',page:3}]},{a:'教材.pdf',b:'课件.pdf'})).toEqual(['教材.pdf · 第 2–3 页','课件.pdf · 第 1 页'])})
 it('handles a complete JSON result followed by a Harness spill notice, and rejects real truncation',()=>{expect(parseToolJson('{"status":"READY"}\n\n(Omitted 100 bytes. Full formatted result stored at: local)')).toEqual({status:'READY'});expect(()=>parseToolJson('{"status":\n\n(Omitted 100 bytes. Full formatted result stored at: local)')).toThrow()})
 it('deduplicates event sequences and binds exact tool answer without assistant rewrite',()=>{
  const head=event(1,'turn/start',{turn:1}),entries=[head,event(2,'user/message',{source:{kind:'user'},content:[{type:'text',text:'问题'}]}),event(3,'tool/call',{callId:'c',name:'noodle_answer_knowledge',arguments:'{}'}),event(4,'tool/result',{message:{content:[{type:'tool-result',toolCallId:'c',isError:false,content:[{type:'text',text:JSON.stringify({status:'READY',answer_markdown:'事实 [K1]',citations:[{citation_id:'K1'}]})}]}]}}),event(5,'turn/end',{reason:{kind:'completed'}})]
  const value=foldEvents([...entries,head])[0];expect(value.users).toEqual(['问题']);expect(value.tools).toHaveLength(1);expect(value.answers[0].citations[0].citation_id).toBe('K1');expect(value.running).toBe(false)
 })
 it('keeps tool business failure distinct from transport errors',()=>{const value=foldEvents([event(1,'turn/start',{turn:1}),event(2,'tool/call',{callId:'c',name:'noodle_answer_knowledge'}),event(3,'tool/result',{message:{content:[{type:'tool-result',toolCallId:'c',isError:false,content:[{type:'text',text:'{"status":"PROFILE_MISMATCH","message":"未就绪"}'}]}]}})])[0];expect(value.answers[0].status).toBe('PROFILE_MISMATCH');expect(value.running).toBe(true)})
 it('preserves only human messages and strips legacy context envelopes',()=>{expect(stripScope('[Noodle scope] {}\n[/Noodle scope]\n用户问题')).toBe('用户问题');const turns=foldEvents([event(1,'turn/start',{turn:1}),event(2,'user/message',{source:{kind:'plugin'},content:[{type:'text',text:'internal context'}]})]);expect(turns[0].users).toEqual([])})
 it('uses DSH RPC, not a direct RAG request',async()=>{const fetcher=vi.fn(async()=>({ok:true,json:async()=>({result:{ok:true,value:{accepted:true}}})}));const client=new AgentClient({fetcher});await client.rpc('session.prompt',{sessionId:'s',mode:'queue',content:[]},{rpcId:'once'});expect(fetcher.mock.calls[0][0]).toBe('/internal/next/agent/rpc/session.prompt');expect(JSON.parse(fetcher.mock.calls[0][1].body).rpcId).toBe('once')})
})

class EventStream {
 readyState=1;onmessage:any;onerror:any;listeners=new Map();close=vi.fn(()=>{this.readyState=2})
 addEventListener(name:string,fn:any){this.listeners.set(name,fn)}
 bridge(){this.listeners.get('bridge')({data:'{"status":"connected"}'})}
 message(payload:any){this.onmessage({data:JSON.stringify({payload})})}
}
describe('DSH live connection recovery',()=>{
 it('uses one combined downlink and clears reconnecting after the upstream bridge recovers',()=>{
  vi.useFakeTimers();const streams:EventStream[]=[],onConnection=vi.fn(),onFrame=vi.fn(),client=new AgentClient({onConnection,onFrame,eventSource:()=>{const s=new EventStream();streams.push(s);return s}})
  client.connect();expect(streams).toHaveLength(1);expect(onConnection).toHaveBeenLastCalledWith('reconnecting');streams[0].bridge()
  expect(onConnection).toHaveBeenLastCalledWith('connected');streams[0].onerror();expect(onConnection).toHaveBeenLastCalledWith('reconnecting')
  streams[0].bridge();expect(onConnection).toHaveBeenLastCalledWith('connected');streams[0].message({type:'session/event'});expect(onFrame).toHaveBeenCalledTimes(1);client.close()
 })
 it('reopens silent or closed downlinks, ignores old stream events, and never resends a prompt',async()=>{
  vi.useFakeTimers();const streams:EventStream[]=[],fetcher=vi.fn(),onFrame=vi.fn(),client=new AgentClient({fetcher,onFrame,eventSource:()=>{const s=new EventStream();streams.push(s);return s}})
  client.connect();streams.forEach(s=>s.bridge());await vi.advanceTimersByTimeAsync(35000)
  expect(streams).toHaveLength(2);expect(streams[0].close).toHaveBeenCalled();streams[0].message({type:'stale'});expect(onFrame).not.toHaveBeenCalled()
  streams[1].bridge();expect(fetcher).not.toHaveBeenCalled();client.close();expect(vi.getTimerCount()).toBe(0)
 })
 it('retains healthy idle downlinks when bridge heartbeats arrive',async()=>{
  vi.useFakeTimers();const streams:EventStream[]=[],client=new AgentClient({eventSource:()=>{const s=new EventStream();streams.push(s);return s}})
  client.connect();for(let i=0;i<5;i++){await vi.advanceTimersByTimeAsync(10000);streams.forEach(s=>s.bridge())}
  expect(streams).toHaveLength(1);client.close()
 })
})
