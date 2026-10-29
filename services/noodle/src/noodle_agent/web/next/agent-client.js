// DSH owns durable messages, tool execution and history. This is a transport/fold only.
export const stripScope=text=>String(text||'').replace(/^\[Noodle scope\][\s\S]*?\[\/Noodle scope\]\n?/, '').trim();
export const contentText=content=>(content||[]).filter(c=>c.type==='text').map(c=>c.text||'').join('\n');
export function mergeSessionEvents(current,snapshot){return [...new Map([...current,...snapshot].map(entry=>[(entry.event||entry).seq,entry])).values()].sort((a,b)=>(a.event||a).seq-(b.event||b).seq);}
export function parseToolJson(text){
 try{return JSON.parse(text);}catch(error){
  // Harness may append a spill notice even when the retained prefix is a
  // complete JSON object. Never attempt to reconstruct a truly truncated one.
  const boundary=text.indexOf('\n\n(Omitted ');
  if(boundary>=0)return JSON.parse(text.slice(0,boundary));
  throw error;
 }
}
export function citationSources(citation,titles={}){
 const groups=new Map();for(const p of citation.provenance||[]){const id=p.source_revision_id||'unknown';if(!groups.has(id))groups.set(id,new Set());if(p.page)groups.get(id).add(p.page);}
 return [...groups].map(([id,values],i)=>{const pages=[...values].sort((a,b)=>a-b),ranges=[];for(let n=0;n<pages.length;n++){const start=pages[n];let end=start;while(pages[n+1]===end+1)end=pages[++n];ranges.push(start===end?String(start):start+'–'+end);}return (titles[id]||('来源资料 '+(i+1)))+(ranges.length?' · 第 '+ranges.join('、')+' 页':'');});
}
export function resultValue(message){
 const result=message?.content?.find(c=>c.type==='tool-result');
 if(!result)return null;
 try{return {callId:result.toolCallId,isError:result.isError,value:parseToolJson(contentText(result.content))};}
 catch{return {callId:result.toolCallId,isError:result.isError,value:{message:contentText(result.content)}};}
}
export function foldEvents(entries,{running}={}){
 const events=[...new Map(entries.map(x=>{const e=x.event||x;return [e.seq,e]})).values()].sort((a,b)=>a.seq-b.seq);
 const turns=[];let turn=null;const calls=new Map(),approvalStarts=new Map(),chunks=new Map();
 for(const e of events){
  if(e.type==='turn/start'){if(turn?.running){turn.running=false;turn.interrupted=true;}turn={id:e.data.turn,seq:e.seq,users:[],texts:[],tools:[],answers:[],running:true,usage:{input:0,cached:0,output:0},time:e.time};turns.push(turn);}
  if(!turn&&['user/message','assistant/chunk','assistant/message','tool/call','tool/result'].includes(e.type)){turn={id:e.data?.turn||0,seq:e.seq,users:[],texts:[],tools:[],answers:[],running:true,usage:{input:0,cached:0,output:0},time:e.time};turns.push(turn);}
  if(!turn)continue;
  const d=e.data||{};
  if(e.type==='assistant/chunk'){
   const key=turn.seq+':'+d.step,blocks=chunks.get(key)||new Map(),chunk=d.chunk||{};
   if(chunk.type==='text-delta')blocks.set(chunk.index,(blocks.get(chunk.index)||'')+(chunk.text||''));
   if(chunk.type==='block-end'&&chunk.block?.type==='text')blocks.set(chunk.index,chunk.block.text||'');
   chunks.set(key,blocks);turn.streamingText=[...blocks].sort(([a],[b])=>a-b).map(([,text])=>text).join('\n');
  }
  if(e.type==='approval/asked')approvalStarts.set(d.id,{time:e.time,turn});
  if(e.type==='approval/decided'){const asked=approvalStarts.get(d.id);if(asked){asked.turn.approvalWaitMs=(asked.turn.approvalWaitMs||0)+Math.max(0,e.time-asked.time);approvalStarts.delete(d.id);}}
  if(e.type==='user/message'&&['user','user-rpc'].includes(d.source?.kind)){const text=stripScope(contentText(d.content));if(text)turn.users.push(text);}
  if(e.type==='assistant/message'){
   if(e.surfaceOp==='replace')continue;
   chunks.delete(turn.seq+':'+d.step);turn.streamingText='';
   const text=contentText(d.message?.content);if(text)turn.texts.push(text);
   if(d.usage){turn.usage.input+=d.usage.inputTokens||0;turn.usage.cached+=d.usage.cacheReadTokens||0;turn.usage.output+=d.usage.outputTokens||0;}
  }
  if(e.type==='tool/call'){const item={id:d.callId,name:d.name,args:d.arguments,time:e.time};calls.set(d.callId,item);turn.tools.push(item);}
  if(e.type==='tool/result'){
   const r=resultValue(d.message);if(!r)continue;const item=calls.get(r.callId);
   if(item){Object.assign(item,{result:r.value,error:r.isError,elapsedMs:e.time-item.time});if(item.name==='noodle_answer_knowledge'&&!r.isError)turn.answers.push(r.value);}
   else if(typeof r.value.answer_markdown==='string'&&Array.isArray(r.value.citations))turn.answers.push(r.value);
  }
  if(e.type==='turn/end'){turn.running=false;turn.elapsedMs=e.time-turn.time;turn.reason=d.reason;}
 }
 // Some DSH failures/cancellations close the live session without turn/end.
 // The authoritative session status prevents an abandoned history from spinning forever.
 const last=turns.at(-1);if(last?.running&&running===false){last.running=false;last.interrupted=true;}
 return turns;
}
export function bindCitationLinks(target,citations,open){
 const byId=new Map(citations.map(c=>[c.citation_id,c]));const walker=document.createTreeWalker(target,NodeFilter.SHOW_TEXT),nodes=[];
 while(walker.nextNode()){const node=walker.currentNode;if(!node.parentElement.closest('a,code,pre,.katex,math'))nodes.push(node);}
 for(const node of nodes){const text=node.textContent,matches=[...text.matchAll(/\[(K[0-9]+)\]/g)];if(!matches.length)continue;let cursor=0;const fragment=document.createDocumentFragment();for(const match of matches){fragment.append(document.createTextNode(text.slice(cursor,match.index)));const citation=byId.get(match[1]);if(citation){const link=document.createElement('a');link.href='#';link.textContent=match[0];link.title=citation.title;link.onclick=e=>{e.preventDefault();open(citation);};fragment.append(link);}else fragment.append(document.createTextNode(match[0]));cursor=match.index+match[0].length;}fragment.append(document.createTextNode(text.slice(cursor)));node.replaceWith(fragment);}
}
export function formatRagUsage(summary={}){
 return [['llm','RAG 文本'],['embedding','嵌入'],['rerank','重排']].flatMap(([key,label])=>{
  const value=summary[key];if(!value?.calls)return [];
  const tokens=Number.isInteger(value.total_tokens)?' · '+value.total_tokens.toLocaleString()+' Token':'';
  return [label+' '+value.calls+' 次'+tokens];
 }).join('；');
}
export class ApprovalInbox{
 pending=new Map();
 apply(frame){
  if(frame.type==='approval/requested'&&frame.approvalId&&frame.rpcId)this.pending.set(frame.approvalId,{...this.pending.get(frame.approvalId),...frame});
  if(frame.type==='approval/resolved')this.pending.delete(frame.approvalId);
 }
 forSession(id){return [...this.pending.values()].filter(value=>value.sessionId===id);}
}
export class AgentClient{
 constructor({base='/internal/next/agent',fetcher=(...args)=>fetch(...args),onFrame=()=>{},onConnection=()=>{},eventSource=url=>new EventSource(url),now=()=>Date.now()}={}){Object.assign(this,{base,fetcher,onFrame,onConnection,eventSource,now});this.streams=[];this.channels=new Map();this.healthTimer=null;this.connection=null;}
 async rpc(method,payload={},extra={}){
  const read=['session.list','session.history','session.search','workspace.list'].includes(method);
  const response=await this.fetcher(this.base+'/rpc/'+method,{method:'POST',cache:'no-store',...(read?{signal:AbortSignal.timeout(12000)}:{}),headers:{'Content-Type':'application/json'},body:JSON.stringify({rpcId:extra.rpcId||crypto.randomUUID(),payload,...extra})});
  const value=await response.json();if(!response.ok)throw Object.assign(Error(value.detail||'DSH 请求失败'),{definite:response.status<500&&response.status!==409});if(!value.result?.ok)throw Object.assign(Error(value.result?.error?.message||'DSH 请求失败'),{definite:true});return value.result.value;
 }
 connectionState(){const value=this.channels.get('all')?.healthy?'connected':'reconnecting';if(value!==this.connection){this.connection=value;this.onConnection(value);}}
 openChannel(channel){
  const old=this.channels.get(channel);if(old){old.stream.close();this.streams=this.streams.filter(s=>s!==old.stream);}
  const stream=this.eventSource(this.base+'/events/'+channel),state={stream,healthy:false,lastSeen:this.now()};this.channels.set(channel,state);this.streams.push(stream);
  const seen=()=>{if(this.channels.get(channel)!==state)return false;state.lastSeen=this.now();state.healthy=true;this.connectionState();return true;};
  stream.addEventListener('bridge',e=>{try{const value=JSON.parse(e.data);if(value.status==='connected')seen();else{state.healthy=false;this.connectionState();}}catch{}});
  stream.onmessage=e=>{if(!seen())return;let frame;try{frame=JSON.parse(e.data);}catch{this.connection=null;this.onConnection('消息解析失败');return;}this.onFrame({...frame.payload,rpcId:frame.rpcId});};
  // SSE onopen only confirms the local HTTP connection, not the DSH WebSocket.
  stream.onerror=()=>{if(this.channels.get(channel)!==state)return;state.healthy=false;this.connectionState();};
 }
 connect(){if(this.streams.length)return;this.openChannel('all');this.connectionState();this.healthTimer=setInterval(()=>this.recover(),5000);}
 recover(){if(!this.streams.length){this.connect();return;}for(const [channel,state] of this.channels)if(this.now()-state.lastSeen>30000||state.stream.readyState===2)this.openChannel(channel);this.connectionState();}
 close(){clearInterval(this.healthTimer);this.healthTimer=null;for(const stream of this.streams)stream.close();this.streams=[];this.channels.clear();this.connection=null;}
 async respond(frame,outcome){const response=await this.fetcher(this.base+'/respond',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:'client-response',rpcId:frame.rpcId,result:{ok:true,value:{sessionId:frame.sessionId,approvalId:frame.approvalId,outcome}}})});const value=await response.json();if(!response.ok)throw Error(value.detail||'审批决定提交失败，请重试。');return value;}
}
