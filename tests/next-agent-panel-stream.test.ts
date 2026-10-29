import {afterEach,expect,it,vi} from 'vitest'
const server=vi.hoisted(()=>({client:null as any,running:false,events:[] as any[],prompts:[] as string[]}))
vi.mock('../services/noodle/src/noodle_agent/web/next/markdown.js',()=>({renderMarkdown:(node:any,text:string)=>{node.textContent=text}}))
vi.mock('../services/noodle/src/noodle_agent/web/next/agent-client.js',async importOriginal=>{
 const original:any=await importOriginal();return {...original,bindCitationLinks:()=>{},AgentClient:class {
  constructor(public config:any){server.client=this}
  connect(){this.config.onConnection('connected')}
  recover(){}close(){}
  async rpc(method:string,payload:any,extra:any){
   if(method==='session.list')return {items:[{sessionId:'s',running:server.running}]};if(method==='workspace.list')return {};if(method==='session.history')return {events:[...server.events]}
   if(method==='session.prompt'){
    server.prompts.push(extra.rpcId);const seq=(server.events.at(-1)?.event.seq||0)+1
    for(const [i,type,data] of [[0,'turn/start',{turn:seq}],[1,'user/message',{source:{kind:'user-rpc'},content:payload.content}],[2,'assistant/message',{step:0,message:{content:[{type:'text',text:'后续回答'}]}}],[3,'turn/end',{reason:{kind:'completed'}}]] as any[])server.events.push({event:{seq:seq+i,type,data,time:seq+i}})
    return {accepted:true}
   }
   throw Error('Unexpected write: '+method)
  }
 }}
})
import {createAgentPanel} from '../services/noodle/src/noodle_agent/web/next/agent-panel.js'
class Element {
 [key:string]:any
 children:Element[]=[];textContent='';hidden=false;value='';className='';scrollHeight=0;scrollTop=0;clientHeight=100
 constructor(public tag:string){}
 get options(){return this.children}get selectedOptions(){return this.children.filter(n=>n.value===this.value)}
 parent:Element|null=null
 append(...nodes:Element[]){for(const n of nodes){n.parent=this;this.children.push(n)}}replaceChildren(...nodes:Element[]){this.children=[...nodes];for(const n of nodes)n.parent=this}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null}
 insertBefore(node:Element,before:Element|null){node.remove();const i=before?this.children.indexOf(before):this.children.length;this.children.splice(i,0,node);node.parent=this}
 setAttribute(){}addEventListener(){}querySelectorAll(){return []}focus(){}select(){}
}
const walk=(node:Element):Element[]=>[node,...node.children.flatMap(walk)]
const event=(seq:number,type:string,data:any)=>({event:{seq,type,data,time:seq*100}})
afterEach(()=>{vi.clearAllTimers();vi.useRealTimers();vi.unstubAllGlobals()})
it('renders chunks before completion, then fills missed completion from history without erasing the draft',async()=>{
 vi.useFakeTimers();server.running=false;server.events=[];server.prompts=[]
 vi.stubGlobal('document',{hidden:false,createElement:(tag:string)=>new Element(tag),addEventListener:()=>{}})
 vi.stubGlobal('window',{addEventListener:()=>{}});vi.stubGlobal('location',{host:'localhost'})
 vi.stubGlobal('localStorage',{getItem:(key:string)=>key.startsWith('noodle.agent.session.')?'s':null,setItem:()=>{}})
 vi.stubGlobal('fetch',async()=>({json:async()=>[]}));vi.stubGlobal('requestAnimationFrame',(fn:any)=>setTimeout(fn,0))
 const panel=createAgentPanel({onCitation:()=>{}});await panel.ready;await vi.advanceTimersByTimeAsync(0)
 const emit=(entry:any)=>{server.events.push(entry);server.client.config.onFrame({type:'session/event',sessionId:'s',event:entry.event})}
 server.running=true;emit(event(1,'turn/start',{turn:1}));emit(event(2,'assistant/chunk',{step:0,chunk:{type:'text-delta',index:0,text:'实时第一段'}}))
 await vi.advanceTimersByTimeAsync(0)
 expect(walk(panel.element).some(n=>n.className.includes('agent-markdown')&&n.textContent==='实时第一段')).toBe(true)
 const draft=walk(panel.element).find(n=>n.tag==='textarea')!;draft.value='下一条草稿'
 // Simulate a lost downlink: DSH completes, but no final live frames reach the panel.
 server.running=false;server.events.push(event(3,'assistant/message',{step:0,message:{content:[{type:'text',text:'完整回答'}]}}),event(4,'turn/end',{reason:{kind:'completed'}}))
 await vi.advanceTimersByTimeAsync(3016)
 expect(walk(panel.element).some(n=>n.className.includes('agent-markdown')&&n.textContent==='完整回答')).toBe(true)
 expect(walk(panel.element).find(n=>n.textContent==='发送')?.disabled).toBe(false);expect(draft.value).toBe('下一条草稿')
 expect(walk(panel.element).some(n=>n.textContent==='正在理解问题…')).toBe(false)
 const historyNode=walk(panel.element).find(n=>n.className.includes('agent-markdown')&&n.textContent==='完整回答')!
 const form=walk(panel.element).find(n=>n.className==='question-form')!
 await form.onsubmit({preventDefault(){}});await vi.advanceTimersByTimeAsync(0)
 expect(walk(panel.element)).toContain(historyNode)
 draft.value='另一条问题';await form.onsubmit({preventDefault(){}});await vi.advanceTimersByTimeAsync(0)
 expect(server.prompts).toHaveLength(2);expect(new Set(server.prompts).size).toBe(2)
})
