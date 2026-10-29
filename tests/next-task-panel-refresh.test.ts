import {afterEach,expect,it,vi} from 'vitest'

// Minimal element surface for exercising the actual drawer renderer in Node.
class Element {
 [key:string]:any
 children:Element[]=[];dataset:any={};attributes=new Map();parentElement:Element|null=null;hidden=false;textContent='';classes=new Set<string>()
 constructor(public tag:string,text='',cls=''){this.textContent=text;this.className=cls}
 set className(value:string){this.classes=new Set(value.split(' ').filter(Boolean))}
 get className(){return [...this.classes].join(' ')}
 classList={contains:(s:string)=>this.classes.has(s),toggle:(s:string,on:boolean)=>{if(on)this.classes.add(s);else this.classes.delete(s)}}
 setAttribute(k:string,v:any){this.attributes.set(k,v)}
 removeAttribute(k:string){this.attributes.delete(k)}
 addEventListener(){}
 append(...nodes:Element[]){for(const node of nodes){node.parentElement=this;this.children.push(node)}}
 replaceChildren(...nodes:Element[]){for(const node of this.children)node.parentElement=null;this.children=[];this.append(...nodes)}
 insertBefore(node:Element,before:Element){if(node.parentElement)node.parentElement.children=node.parentElement.children.filter(n=>n!==node);node.parentElement=this;const at=this.children.indexOf(before);if(at<0)this.children.push(node);else this.children.splice(at,0,node)}
 querySelectorAll(selector:string):Element[]{const key=selector==='[data-task-stage]'?'taskStage':'stageTokens';return this.children.flatMap(n=>[...(n.dataset[key]?[n]:[]),...n.querySelectorAll(selector)])}
}
const all=(node:Element):Element[]=>[node,...node.children.flatMap(all)]
afterEach(()=>{vi.clearAllTimers();vi.useRealTimers();vi.unstubAllGlobals()})

it('updates stage, terminal status, counters and controls while the same drawer stays open',async()=>{
 vi.useFakeTimers();vi.resetModules();const ids=new Map(['drawerBody','job-drawer','drawerSummary','jobPulse'].map(id=>[id,new Element('div')]))
 vi.stubGlobal('document',{hidden:false,getElementById:(id:string)=>ids.get(id)})
 const job={id:'job',created_at:'2026-10-29',status:'RUNNING',source_title:'ML6',source_revision_id:'revision'}
 let finished=false;const fetchImpl=vi.fn(async(url:string,options:any)=>({ok:true,json:async()=>url.endsWith('/generation-tasks')?{items:[job],next_cursor:null}:{job:{...job,status:finished?'SUCCEEDED':'RUNNING'},control:{state:'ACTIVE'},summary:{active_ms:finished?12000:3000,ticking:!finished},stages:[{stage:'plan',label:'规划',status:finished?'COMPLETED':'RUNNING',wall_ms:finished?12000:3000,ticking:!finished,total_tokens:finished?22:0}]}}))
 vi.stubGlobal('fetch',fetchImpl);const {renderTaskDrawer}=await import('../services/noodle/src/noodle_agent/web/next/task-panel.js')
 const el=(tag:string,text?:string,cls?:string)=>new Element(tag,text,cls),ui={el,action:(label:string,fn:any)=>Object.assign(el('button',label),{onclick:fn})}
 await renderTaskDrawer([],ui);const host=ids.get('drawerBody')!,card=host.children.find(n=>n.dataset.taskId==='job')!
 expect(all(card).find(n=>n.className==='task-state')?.textContent).toBe('生成中');expect(all(card).some(n=>n.textContent==='暂停')).toBe(true)
 finished=true;await renderTaskDrawer([],ui)
 expect(host.children.find(n=>n.dataset.taskId==='job')).toBe(card)
 expect(all(card).find(n=>n.className==='task-state')?.textContent).toBe('笔记已生成');expect(all(card).some(n=>n.textContent==='暂停')).toBe(false)
 expect(all(card).some(n=>n.textContent==='已处理')).toBe(true);expect(all(card).some(n=>n.textContent==='22 Token')).toBe(true)
 expect(ids.get('drawerSummary')?.textContent).toBe('1个任务')
 expect(fetchImpl.mock.calls.every(([,options])=>options.cache==='no-store'&&options.signal instanceof AbortSignal)).toBe(true)
})
