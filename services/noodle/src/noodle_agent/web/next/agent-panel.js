import {AgentClient,foldEvents,mergeSessionEvents,bindCitationLinks,citationSources,formatRagUsage,ApprovalInbox} from './agent-client.js';
import {createProgressPolling} from './task-polling.js';
import {renderMarkdown} from './markdown.js';
import {QuestionContext} from './dialogue.js';
import {reconcileChildren} from './dom-reconcile.js';

const labels={noodle_answer_knowledge:'检索并回答',noodle_search_knowledge:'查找知识',noodle_get_atomic_note:'读取笔记小节',noodle_get_concept:'读取知识点',noodle_list_sources:'查找资料',noodle_list_tasks:'查找任务',noodle_edit_section:'修改笔记',noodle_get_readiness:'检查知识服务'};
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const button=(text,fn,cls)=>{const b=el('button',text,cls);b.type='button';b.onclick=()=>Promise.resolve(fn()).catch(e=>error(e.message));return b;};
let error=()=>{};
export function createAgentPanel({onCitation,onContext=()=>{},onSession=()=>{}}){
 const root=el('section',undefined,'qa-panel dsh-panel'),toolbar=el('div',undefined,'agent-toolbar'),sessions=el('select'),history=el('div',undefined,'agent-history'),log=el('div',undefined,'assistant-log'),form=el('form',undefined,'question-form'),input=el('textarea'),scope=el('select'),mode=el('select'),send=el('button','发送','primary'),status=el('p','','agent-status'),context=new QuestionContext();
 let sessionId=null,entries=[],generation=0,running=false,submitting=false,connected=false,loaded=false,pendingRpc=null,selectedNote=null,pinnedNote=null,sourceItems=[],lastSeq=-1,renderPending=false;
 const drafts=new Map(),scopes=new Map(),turnNodes=new Map();
 let sessionRunning=null,stopRequested=false,syncPolling=null,syncDelayed=false;
 const approvalInbox=new ApprovalInbox(),approvalArea=el('section',undefined,'agent-approval-area');approvalArea.setAttribute('aria-label','待审批操作');approvalArea.setAttribute('aria-live','polite');
 const storageKey='noodle.agent.session.'+location.host;
 try{sessionId=localStorage.getItem(storageKey);}catch{}
 input.id='question';input.rows=3;input.maxLength=4000;input.placeholder='询问知识库，或查找笔记与任务…';input.setAttribute('aria-label','你的问题');send.type='submit';
 sessions.setAttribute('aria-label','历史对话');sessions.onchange=()=>switchSession(sessions.value).catch(e=>error(e.message));
 const historySearch=el('input');historySearch.placeholder='搜索对话标题';historySearch.setAttribute('aria-label','搜索历史对话');historySearch.oninput=()=>{for(const option of sessions.options)option.hidden=!option.textContent.toLowerCase().includes(historySearch.value.toLowerCase());};
 history.append(historySearch,sessions);history.hidden=true;
 const renameForm=el('form',undefined,'agent-rename'),renameInput=el('input'),renameSave=el('button','保存');renameInput.setAttribute('aria-label','对话名称');renameInput.maxLength=120;renameInput.required=true;renameSave.type='submit';renameForm.hidden=true;
 renameForm.append(renameInput,renameSave,button('取消',()=>{renameForm.hidden=true;}));
 renameForm.onsubmit=async event=>{event.preventDefault();const id=sessionId,title=renameInput.value.trim();if(!id||!title)return;renameSave.disabled=true;try{await client.rpc('session.rename',{sessionId:id,title});await refreshSessions();renameForm.hidden=true;}catch(e){error(e.message);}finally{renameSave.disabled=false;}};
 const older=button('更早消息',async()=>{if(!sessionId||!entries.length)return;const id=sessionId,ticket=generation;const beforeSeq=Math.min(...entries.map(x=>(x.event||x).seq));const value=await client.rpc('session.history',{sessionId:id,beforeSeq,maxMessages:24});if(ticket!==generation)return;entries=[...(value.events||[]),...entries];older.hidden=!value.hasMore;queueRender();});older.hidden=true;
 const stop=button('停止',async()=>{if(sessionId){stopRequested=true;try{await client.rpc('session.cancel',{sessionId});await refreshSessions();status.textContent='已请求停止当前回答';}catch(e){stopRequested=false;throw e;}}},'agent-stop');stop.hidden=true;
 toolbar.append(button('新对话',()=>newSession()),button('历史',()=>{history.hidden=!history.hidden;return refreshSessions();}),button('重命名',()=>{if(!sessionId)return;renameInput.value=sessions.selectedOptions[0]?.textContent||'';renameForm.hidden=false;renameInput.focus();renameInput.select();}),button('归档',async()=>{if(!sessionId)return;await client.rpc('workspace.archiveSession',{sessionId});sessionId=null;await refreshSessions();await newSession();}));
 scope.setAttribute('aria-label','问答范围');mode.setAttribute('aria-label','检索方式');for(const [v,t] of [['HYBRID','混合检索'],['KEYWORD_ONLY','关键词检索']]){const o=el('option',t);o.value=v;mode.append(o);}
 scope.onchange=()=>{context.clear();if(scope.value==='NOTES')pinnedNote=pinnedNote||selectedNote;else pinnedNote=null;rememberScope();};mode.onchange=rememberScope;
 function rememberScope(){if(sessionId&&loaded){const preference={scope:scope.value,mode:mode.value,note:pinnedNote};scopes.set(sessionId,preference);try{localStorage.setItem('noodle.agent.scope.'+sessionId,JSON.stringify(preference));}catch{}}onContext(scope.selectedOptions[0]?.textContent||'整个知识库');}
 function queryScope(){if(!scope.value)throw Error('请选择有效的问答范围。');return scope.value==='NOTES'&&(pinnedNote||selectedNote)?{mode:'NOTES',note_ids:[(pinnedNote||selectedNote).id]}:scope.value.startsWith('SOURCE:')?{mode:'SOURCE',source_ids:[scope.value.slice(7)]}:{mode:'GLOBAL'};}
 function updateChoices(){const old=scope.value||'GLOBAL';scope.replaceChildren();for(const [value,title] of [['GLOBAL','整个知识库'],...((pinnedNote||selectedNote)?[['NOTES','笔记：'+(pinnedNote||selectedNote).title]]:[]),...sourceItems.map(s=>['SOURCE:'+s.id,'资料：'+s.title])]){const o=el('option',title);o.value=value;scope.append(o);}scope.value=[...scope.options].some(o=>o.value===old)?old:'GLOBAL';rememberScope();}
 function setRunning(value){running=value;send.disabled=value||submitting;stop.hidden=!value;scope.disabled=value||submitting;mode.disabled=value||submitting;}
 error=message=>{status.textContent=message;status.className='agent-status status-error';};
 function queueRender(){if(renderPending)return;renderPending=true;requestAnimationFrame(()=>{renderPending=false;render();});}
 function renderApprovals(){
  approvalArea.replaceChildren();
  for(const frame of approvalInbox.forSession(sessionId)){
   const card=el('section',undefined,'agent-approval');card.append(el('strong','确认操作'),el('p',frame.reason||labels[frame.toolName]||frame.toolName));
   const tool=foldEvents(entries).flatMap(t=>t.tools).find(t=>t.id===frame.callId);let parameters=tool?.args;
   if(typeof parameters==='string'){try{parameters=JSON.parse(parameters);}catch{}}
   if(parameters){const detail=el('details');detail.append(el('summary','查看操作内容'),el('pre',typeof parameters==='string'?parameters:JSON.stringify(parameters,null,2)));card.append(detail);}
   if(frame.responding)card.append(el('p','正在提交审批决定…'));
   for(const [label,outcome] of [['允许一次','allowed-once'],['拒绝','rejected']]){const b=button(label,async()=>{frame.responding=true;renderApprovals();try{await client.respond(frame,outcome);}catch(e){frame.responding=false;renderApprovals();throw e;}});b.disabled=!!frame.responding;card.append(b);}
   approvalArea.append(card);
  }
 }
 function render(){const keepBottom=log.scrollHeight-log.scrollTop-log.clientHeight<70,oldTop=log.scrollTop;const nodes=[],seen=new Set();const turns=foldEvents(entries,{running:sessionRunning});let active=false;
  if(!turns.length)nodes.push(el('div','直接询问你的笔记，或让助手查找资料、查看进度。','assistant-welcome'));
  for(const turn of turns){
   const key=generation+':'+turn.seq,signature=JSON.stringify([turn,sourceItems.map(s=>[s.current_revision_id,s.original_name||s.title])]);seen.add(key);
   const previous=turnNodes.get(key);if(previous?.signature===signature){nodes.push(previous.node);active=active||turn.running;continue;}
   const turnLog=el('div',undefined,'agent-turn');nodes.push(turnLog);turnNodes.set(key,{signature,node:turnLog});
   for(const text of turn.users)turnLog.append(el('div',text,'chat-entry user'));
   if(turn.answers.length){for(const answer of turn.answers){const card=el('section',undefined,'chat-entry agent-answer'),text=el('div',undefined,'agent-markdown');renderMarkdown(text,answer.answer_markdown||answer.message||answer.coverageWarning||'未取得可用答案');card.append(text);bindCitationLinks(text,answer.citations||[],onCitation);
    if(answer.status&&answer.status!=='READY')card.append(el('p',answer.status,'agent-business-status status-error'));
    for(const citation of answer.citations||[]){const b=button('['+citation.citation_id+'] '+citation.title,()=>onCitation(citation),'citation');const titles={...Object.fromEntries(sourceItems.map(s=>[s.current_revision_id,s.original_name||s.title])),...(answer.source_titles||{})};for(const label of citationSources(citation,titles))b.append(el('small',label));card.append(b);}
    // Inline citation anchors share the exact immutable citation object.
    for(const link of text.querySelectorAll('a')){const id=(link.textContent||'').replace(/[\[\]]/g,'');const c=(answer.citations||[]).find(c=>c.citation_id===id);if(c)link.onclick=e=>{e.preventDefault();onCitation(c);};}
    const usage=formatRagUsage(answer.usage_summary);if(usage)card.append(el('small',usage,'agent-turn-usage'));
    turnLog.append(card);}}
   else if(turn.streamingText||turn.texts.length){const text=el('div',undefined,'chat-entry agent-markdown');renderMarkdown(text,turn.streamingText||turn.texts.at(-1));turnLog.append(text);}
   if(turn.tools.length){const details=el('details',undefined,'agent-tool-list');details.append(el('summary',turn.tools.length+' 项工具操作'));for(const tool of turn.tools){const row=el('details',undefined,'agent-tool');const failed=tool.error||(tool.result?.status&&tool.result.status!=='READY');row.append(el('summary',(labels[tool.name]||tool.name.replace('noodle_',''))+' · '+(tool.result?(failed?'受阻':'完成'):turn.running?'执行中':'未完成')+(tool.elapsedMs?' · '+(tool.elapsedMs/1000).toFixed(1)+'秒':'')));row.append(el('pre',JSON.stringify({parameters:tool.args,result:tool.result},null,2)));details.append(row);}turnLog.append(details);}
   if(turn.interrupted||['aborted','interrupted'].includes(turn.reason?.kind))turnLog.append(el('p','本次执行已结束，未取得完整回答。可以继续提问。','agent-progress'));
   if(turn.running){active=true;turnLog.append(el('p',turn.tools.length?'正在执行工具…':'正在理解问题…','agent-progress'));}
   else if(turn.elapsedMs)turnLog.append(el('small',(turn.elapsedMs/1000).toFixed(1)+' 秒'+(turn.approvalWaitMs?'（审批等待 '+(turn.approvalWaitMs/1000).toFixed(1)+' 秒）':'')+' · Agent 输入 '+(turn.usage.input+turn.usage.cached).toLocaleString()+' Token（缓存 '+turn.usage.cached.toLocaleString()+'）· 输出 '+turn.usage.output.toLocaleString(),'agent-turn-usage'));
  }
  for(const key of turnNodes.keys())if(!seen.has(key))turnNodes.delete(key);
  reconcileChildren(log,nodes);setRunning(active);renderApprovals();if(keepBottom)log.scrollTop=log.scrollHeight;else log.scrollTop=oldTop;
 }
 async function refreshSessions(){const ticket=generation,seq=lastSeq,id=sessionId;const [value,workspaces]=await Promise.all([client.rpc('session.list'),client.rpc('workspace.list')]);const archived=new Set(workspaces.archivedSessionIds||[]);const items=(value.items||[]).filter(s=>!archived.has(s.sessionId)).sort((a,b)=>(b.updatedAt||0)-(a.updatedAt||0));if(ticket!==generation)return items;sessions.replaceChildren();for(const item of items){if(item.origin==='subagent')continue;const title=item.projections?.values?.title;const o=el('option',typeof title==='string'?title:title?.title||'新对话');o.value=item.sessionId;sessions.append(o);}if(sessionId===id&&sessionId&&items.some(s=>s.sessionId===sessionId)){sessions.value=sessionId;if(seq===lastSeq){const value=!!items.find(s=>s.sessionId===sessionId)?.running,changed=value!==sessionRunning;sessionRunning=value;setRunning(value);if(loaded&&changed)queueRender();}}return items;}
 async function hydrate(){if(!sessionId)return;const ticket=generation,id=sessionId;const value=await client.rpc('session.history',{sessionId:id,maxMessages:24});if(ticket!==generation||id!==sessionId)return;older.hidden=!value.hasMore;const merged=mergeSessionEvents(entries,value.events||[]),tail=Math.max(-1,...merged.map(x=>(x.event||x).seq)),changed=!loaded||merged.length!==entries.length||tail!==lastSeq;entries=merged;lastSeq=tail;loaded=true;if(changed){const answer=foldEvents(entries).flatMap(t=>t.answers).at(-1);if(answer)context.accept(context.begin('恢复会话',queryScope()),answer);queueRender();}}
 async function switchSession(id){if(id===sessionId&&loaded)return;if(sessionId)drafts.set(sessionId,input.value);sessionId=id;onSession(id);generation++;loaded=false;sessionRunning=null;stopRequested=false;status.textContent='';older.hidden=true;entries=[];lastSeq=-1;context.clear();pendingRpc=null;input.value=drafts.get(id)||'';try{localStorage.setItem(storageKey,id);}catch{}let saved=scopes.get(id);try{saved=saved||JSON.parse(localStorage.getItem('noodle.agent.scope.'+id));}catch{}if(saved){pinnedNote=saved.note||null;updateChoices();scope.value=saved.scope;mode.value=saved.mode;}else{pinnedNote=null;updateChoices();scope.value='GLOBAL';mode.value='HYBRID';}rememberScope();await hydrate();await refreshSessions();}
 async function newSession(){const value=await client.rpc('session.create',{agentPreset:'noodle-knowledge'});await refreshSessions();await switchSession(value.sessionId);sessions.value=value.sessionId;input.focus();}
 const client=new AgentClient({onFrame(frame){
  if(frame.type==='host/session-added'||frame.type==='host/session-status')void syncPolling?.request();
  if(frame.type==='host/agent-error'&&frame.sessionId===sessionId){sessionRunning=false;queueRender();if(stopRequested)status.textContent='当前回答已停止';else error(frame.error?.message||'Agent 执行失败');}
  if(frame.type==='session/event'&&frame.sessionId===sessionId&&frame.event.type==='turn/start'){sessionRunning=true;stopRequested=false;status.textContent='';void syncPolling?.request();}
  if(frame.type==='session/event'&&frame.sessionId===sessionId){const e=frame.event;if(e.seq>lastSeq){entries.push({event:e,view:frame.view});lastSeq=e.seq;const chunk=e.data?.chunk;if(['turn/start','turn/end','user/message','assistant/message','tool/call','tool/result'].includes(e.type)||e.type==='assistant/chunk'&&(chunk?.type==='text-delta'||chunk?.type==='block-end'&&chunk.block?.type==='text'))queueRender();if(e.type==='turn/end'){sessionRunning=false;const answer=foldEvents(entries).flatMap(t=>t.answers).at(-1);if(answer)context.accept(context.begin('继续',queryScope()),answer);pendingRpc=null;void syncPolling?.request();}}}
  if(frame.type==='approval/requested'||frame.type==='approval/resolved'){approvalInbox.apply(frame);renderApprovals();}
 },onConnection(value){if(value==='connected'){connected=true;status.textContent='';status.className='agent-status';void syncPolling?.request();}else{connected=false;status.textContent=value==='reconnecting'?'消息连接恢复中，正在同步会话…':value;}}});
 form.onsubmit=async e=>{
  e.preventDefault();const submittedText=input.value,question=submittedText.trim();if(!question||running||submitting)return;
  submitting=true;setRunning(running);let sentSession=null;
  try{
   if(!sessionId)await newSession();sentSession=sessionId;
   const payloadScope=queryScope(),hints=context.begin(question,payloadScope);pendingRpc=pendingRpc||crypto.randomUUID();send.disabled=true;status.textContent='正在发送…';
   await client.rpc('session.prompt',{sessionId:sentSession,mode:'queue',content:[{type:'text',text:question}],clientTimeZone:Intl.DateTimeFormat().resolvedOptions().timeZone},{rpcId:pendingRpc,context:{scope:payloadScope,retrieval_mode:mode.value,entity_note_ids:hints.entity_note_ids}});
   if(sentSession===sessionId){
    // Acknowledged delivery needs no replay. A lost turn/end must not cause
    // the next distinct question to reuse this request's idempotency key.
    pendingRpc=null;if(input.value===submittedText)input.value='';drafts.set(sessionId,input.value);status.textContent='';sessionRunning=true;setRunning(true);
    await hydrate();queueRender();void syncPolling.request();
   }
  }catch(e){if(!sentSession||sentSession===sessionId){if(e.definite)pendingRpc=null;error(e.message+' 问题已保留；如发送状态待确认，请先刷新会话。');}}
  finally{submitting=false;setRunning(running);}
 };
 form.append(scope,mode,input,send,stop);root.append(toolbar,history,renameForm,older,log,approvalArea,status,form);
 const sourceReady=fetch('/internal/next/sources',{cache:'no-store',signal:AbortSignal.timeout(12000)}).then(r=>r.json()).then(items=>{sourceItems=items.filter(s=>s.kind==='PDF'&&!s.deleted_at);updateChoices();}).catch(()=>{});updateChoices();
 const ready=(async()=>{if(!document.hidden)client.connect();await sourceReady;const items=await refreshSessions();if(sessionId&&items.some(i=>i.sessionId===sessionId))await switchSession(sessionId);else{sessionId=null;render();}})().catch(e=>error(e.message));
 syncPolling=createProgressPolling({refresh:async()=>{const items=await refreshSessions();if(sessionId&&items.some(i=>i.sessionId===sessionId)){if(loaded)await hydrate();else await switchSession(sessionId);}if(syncDelayed&&connected){status.textContent='';status.className='agent-status';}syncDelayed=false;},visible:()=>!document.hidden,expanded:()=>sessionRunning||running||submitting,onError:()=>{syncDelayed=true;status.textContent=connected?'会话同步延迟，正在重试…':'消息连接恢复中，正在同步会话…';}});
 void ready.then(()=>syncPolling.start());
 const recover=()=>{if(document.hidden){client.close();return;}client.recover();void syncPolling.start();};
 document.addEventListener('visibilitychange',recover);window.addEventListener('focus',recover);window.addEventListener('online',recover);window.addEventListener('pageshow',recover);
 window.addEventListener('pagehide',()=>syncPolling.stop());
 window.addEventListener('pagehide',()=>client.close());
 return {element:root,ready,get sessionId(){return sessionId;},setNote(note){selectedNote=note&&note.kind!=='MOC'?note:null;if(scope.value!=='NOTES')pinnedNote=null;updateChoices();},selectSource(id){scope.value='SOURCE:'+id;rememberScope();},selectNote(note){selectedNote=note;pinnedNote=note;updateChoices();scope.value='NOTES';rememberScope();},focus(){input.focus();}};
}
