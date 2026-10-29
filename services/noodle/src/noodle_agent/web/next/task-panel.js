// Stable headers: polling updates data, the local clock updates text only.
import {duration,elapsedMs,waitingMs,visibleUsage,chapterHint,backgroundItems,taskIssue,archivedTask} from './task-view.js';
import {workspaceApi as request} from './request.js';
import {createProgressPolling,snapshotNow,snapshotStale} from './task-polling.js';
const progressRequest=path=>request(path,{cache:'no-store',signal:AbortSignal.timeout(12000)});
const taskCards=new Map();let streamBusy=false,historyCursor=null,historyButton=null,clockTimer=null,archivedGroup=null,archivedHost=null,archivedSummary=null,poller=null;
export function startTaskPolling(ui){
 const polling=createProgressPolling({refresh:()=>refreshTaskDrawer([],ui),visible:()=>!document.hidden,expanded:()=>!document.getElementById('job-drawer').classList.contains('collapsed')});poller=polling;
 const wake=()=>{void polling.wake();};document.addEventListener('visibilitychange',wake);window.addEventListener('focus',wake);window.addEventListener('online',wake);
 void polling.start();return ()=>{polling.stop();if(poller===polling)poller=null;document.removeEventListener('visibilitychange',wake);window.removeEventListener('focus',wake);window.removeEventListener('online',wake);};
}
export function renderTaskDrawer(jobs,ui){return poller?poller.request():refreshTaskDrawer(jobs,ui);}
const statusNames={COMPLETED:'已处理',REUSED:'已复用',AVAILABLE:'已有结果',RUNNING:'进行中',WAITING:'等待',PARTIAL:'部分完成',PAUSED:'已暂停',REVIEW:'待处理'};
export function taskOrder(items){return [...items].sort((a,b)=>b.created_at.localeCompare(a.created_at)||b.id.localeCompare(a.id));}
function tick(){for(const card of taskCards.values())if(card.timeline){const now=performance.now(),freshNow=snapshotNow(card.received,now),summary=card.timeline.summary,stale=snapshotStale(card.received,now)&&(summary.ticking||summary.waiting_ticking||card.timeline.stages.some(s=>s.ticking));const ms=elapsedMs(summary,card.received,freshNow),wait=waitingMs(summary,card.received,freshNow);card.time.textContent=ms==null?'未记录':duration(ms);card.waitTime.hidden=wait<1000;card.waitTime.textContent='调度等待 '+duration(wait);card.node.classList.toggle('progress-stale',!!stale);card.freshness.hidden=!stale;card.freshness.textContent='进度更新延迟，正在重新连接';if(ms==null)card.time.removeAttribute('datetime');else card.time.dateTime='PT'+Math.floor(ms/1000)+'S';for(const time of card.body.querySelectorAll('[data-task-stage]')){const stage=card.timeline.stages.find(s=>s.stage===time.dataset.taskStage);if(stage)time.textContent=stage.timing_available===false?'用时未记录':duration(elapsedMs({active_ms:stage.wall_ms,ticking:stage.ticking},card.received,freshNow));}}}
function stageDetail(stage,el){const detail=el('small',undefined,'task-stage-metric'),time=el('time'),tokens=el('span');time.dataset.taskStage=stage.stage;tokens.dataset.stageTokens=stage.stage;detail.append(time,tokens);detail.title='阶段用时含模型响应等待，已扣除暂停；并行阶段不能直接相加。Token在供应商返回用量后记入，等待期间不代表零消耗。';return detail;}
function refreshStageDetails(card){for(const node of card.body.querySelectorAll('[data-stage-tokens]')){const s=card.timeline.stages.find(s=>s.stage===node.dataset.stageTokens);if(s)node.textContent=s.usage_available===false?'用量未记录':s.status==='RUNNING'&&!s.total_tokens&&['plan','planning','write','notes','concept'].includes(s.stage)?'用量待响应返回':s.total_tokens>0||!s.missing_usage?s.total_tokens.toLocaleString()+' Token':'';}tick();}
async function retryTask(card,ui,button){
 if(card.retrying)return;card.retrying=true;button.disabled=true;const label=button.textContent;button.textContent='正在重试…';
 try{await request('/jobs/'+card.job.id+'/retry',{method:'POST'});card.bodyKey=null;await loadCard(card,ui,true);}
 catch(e){card.status.textContent=e.message;}
 finally{card.retrying=false;if(button.isConnected){button.disabled=false;button.textContent=label;}}
}
function renderControls(card,ui){
 if(archivedTask(card.job)){card.controls.replaceChildren();card.controlsKey=null;return;}
 const t=card.timeline,paused=t.job.status==='PAUSED'||JSON.parse(t.job.error_json||'{}').code==='USER_PAUSED',draining=t.control.state==='PAUSE_REQUESTED',key=[t.job.status,t.control.state].join(':');
 if(key===card.controlsKey)return;card.controlsKey=key;card.controls.replaceChildren();
 if(paused||['RUNNING','PENDING','RETRY'].includes(t.job.status)){
  const button=ui.action(paused?'继续生成':draining?'章末暂停中…':'暂停',async()=>{button.disabled=true;try{await request('/jobs/'+t.job.id+'/'+(paused?'resume':'pause'),{method:'POST'});await loadCard(card,ui,true);}catch(e){card.status.textContent=e.message;}finally{if(button.isConnected)button.disabled=draining;}});
  button.className='task-pause';button.disabled=draining;button.title=paused?'从已有检查点继续':'当前章节结束后暂停';card.controls.append(button);
 }
}
function renderHeader(card,ui){
 const t=card.timeline;card.title.textContent=card.job.source_title||'PDF';
 card.clockLabel.textContent=t.summary.clock_basis==='HISTORICAL_OBSERVED_SPAN'?'历史记录跨度':'执行用时';card.time.title=t.summary.execution_note||(t.summary.clock_basis==='RECORDED_EXECUTION_ATTEMPTS'?'各次实际执行区间之和；等待、暂停及后续知识维护单独统计。'+(t.summary.notes_ready_wall_ms!=null?' 从导入到笔记索引就绪：'+duration(t.summary.notes_ready_wall_ms):''):'历史观察跨度，含缺少执行边界的等待，不能视为净生成时间。');
 if(t.summary.clock_basis==='QUEUED_NOT_STARTED')card.time.title='尚未开始执行；排队等待单独记录。';
 const revision=card.job.source_revision_id||t.readiness?.source_revision_id;if(revision&&!archivedTask(card.job))card.title.href='#source='+encodeURIComponent(revision);else{card.title.removeAttribute('href');card.title.title='历史任务，资料已移出或已有新版本';}
 card.usage.replaceChildren();const groups=t.summary.imported_generation?[...visibleUsage({model_usage:t.summary.original_model_usage}).map(g=>({...g,label:'原生成 '+g.label})),...visibleUsage(t.summary).map(g=>({...g,label:'本轮维护 '+g.label}))]:visibleUsage(t.summary);for(const group of groups){
  const item=ui.el('span',undefined,'task-model-usage');item.append(ui.el('span',group.label),ui.el('b',group.total_tokens.toLocaleString()));
  item.title=group.models.filter(m=>m.total_tokens>0).map(m=>m.model+'：'+m.total_tokens.toLocaleString()+' Token').join('\n');card.usage.append(item);
 }
 card.usage.hidden=!card.usage.children.length;if(card.usage.children.length)card.usage.append(ui.el('span','Token','task-token-unit'));
 card.status.textContent=t.job.status==='RUNNING'?'生成中':t.job.status==='PENDING'&&t.stages.some(s=>s.stage==='parse'&&s.status==='RUNNING')?'解析中':t.job.status==='PAUSED'?'已暂停':t.job.status==='SUCCEEDED'?(t.readiness?.coverage?.deferred?'部分完成，'+t.readiness.coverage.deferred+'项待恢复':'笔记已生成'):t.job.status==='REVIEW'?'有待处理项目':t.job.status==='RETRY'?'等待重试':'等待处理';
 card.node.dataset.status=t.job.status;card.time.dateTime='PT'+Math.floor((t.summary.active_ms||0)/1000)+'S';renderControls(card,ui);tick();
}
function renderBody(card,ui){
 const t=card.timeline;if(!t||card.body.hidden)return;const {el,action}=ui,state=t.readiness,hint=chapterHint(t),deferred=state?.coverage?.deferred||0;
 const key=JSON.stringify([t.job.status,t.control.state,hint,deferred,t.summary.visual_cache_hits,state?.coverage?.published,state?.coverage?.indexed,state?.notes_status,state?.concept_maintenance,[state?.knowledge_structure?.status,state?.knowledge_structure?.local_status,state?.knowledge_structure?.pending,state?.knowledge_structure?.upstream_pending],t.stages.map(s=>[s.stage,s.status]),t.job.error_json]);
 if(card.bodyKey===key)return;card.bodyKey=key;
 const status=el('div',undefined,'task-current');status.append(el('span',hint||(t.summary.clock_basis==='QUEUED_NOT_STARTED'?'等待任务调度，尚未开始执行':({READY:'笔记生成完成，可浏览与检索',INDEX_PENDING:'笔记已生成，正在建立索引',PARTIAL:'已生成的内容可通过标题打开'}[state?.notes_status]||'按资料规划生成笔记'))));
 if(state?.coverage?.published)status.append(el('small',state.coverage.published+'篇笔记可浏览'));
 if(t.summary.execution_note)status.append(el('small',t.summary.execution_note,'task-history-note'));
 if(t.summary.visual_cache_hits)status.append(el('small','复用视觉缓存 '+t.summary.visual_cache_hits+' 次'));
 const track=el('div',undefined,'stage-track');track.setAttribute('aria-label','生成阶段');
 for(const stage of t.stages.filter(s=>!['concept','index','organization'].includes(s.stage))){const item=el('div',undefined,'stage-pill '+stage.status.toLowerCase());item.append(el('strong',stage.label==='笔记'?'生成笔记':stage.label),el('span',statusNames[stage.status]||stage.status),stageDetail(stage,el));track.append(item);}
 if(state){const ready=state.coverage.indexed===state.coverage.published&&state.coverage.published>0,item=el('div',undefined,'stage-pill '+(ready?'completed':'waiting'));item.append(el('strong','笔记索引'),el('span',state.coverage.indexed+'/'+state.coverage.published+'篇笔记就绪'),el('small','用时与用量见下方索引合计'));track.append(item);}
 const background=el('section',undefined,'task-knowledge');background.setAttribute('aria-label','后台知识整理');const heading=el('div',undefined,'task-knowledge-label'),name=el('div',undefined,'task-knowledge-name');name.append(el('strong','知识整理'),el('span','后台并行更新'));heading.append(name);background.append(heading);
 const knowledgeTrack=el('div',undefined,'stage-track task-knowledge-track');knowledgeTrack.setAttribute('aria-label','知识整理阶段');
 for(const [i,entry]of backgroundItems(state).entries()){const item=el('div',undefined,'stage-pill task-knowledge-item '+entry.status.toLowerCase());item.append(el('strong',i===1?'向量索引（合计）':entry.label),el('span',entry.value));if(entry.note)item.append(el('small',entry.note));const stage=t.stages.find(s=>s.stage===['concept','index','organization'][i]);if(stage){const detail=stageDetail(stage,el);if(i===1)detail.title+='索引调用含笔记与知识点，仅在这里统计一次；共享调用不伪造逐文档分摊。';item.append(detail);}knowledgeTrack.append(item);}background.append(knowledgeTrack);
 const children=[status,track,background];
 if(t.job.status==='REVIEW')children.push(el('p',taskIssue(t),'task-error'));
 if(!archivedTask(card.job)&&(t.job.status==='REVIEW'||['PAUSED','SUCCEEDED'].includes(t.job.status)&&deferred)){
  const line=el('div',undefined,'task-retry-actions');if(deferred)line.append(el('span',deferred+'个范围待补充'));const retry=action(deferred?'重试未完成范围':'检查后重试',()=>retryTask(card,ui,retry));retry.disabled=!!card.retrying;line.append(retry);children.push(line);
 }
 card.body.replaceChildren(...children);
}
async function loadCard(card,ui,force=false){
 if(card.loading){card.again||=force;return;}
 const active=['RUNNING','PENDING','RETRY'].includes(card.job.status);
 if(!force&&card.timeline&&!active&&card.body.hidden&&card.lastStatus===card.job.status)return;
 card.loading=true;try{const t=await progressRequest('/jobs/'+card.job.id+'/timeline');card.timeline=t;card.job={...card.job,...t.job};card.received=performance.now();card.lastStatus=t.job.status;card.status.removeAttribute('title');renderHeader(card,ui);renderBody(card,ui);refreshStageDetails(card);}catch(e){card.status.textContent='进度读取失败，自动重连中';card.status.title=e.message;}finally{card.loading=false;if(card.again){card.again=false;void loadCard(card,ui,true);}}
}
function createCard(job,ui){
 const {el,action}=ui,node=el('section',undefined,'generation-task'),heading=el('header',undefined,'task-heading'),body=el('div',undefined,'task-content');body.id='task-content-'+job.id;
 const title=el('a',job.source_title||'PDF','task-title'),status=el('span','正在读取','task-state'),freshness=el('small','','task-wait-time'),name=el('div',undefined,'task-name');freshness.hidden=true;freshness.setAttribute('role','status');name.append(title,status,freshness);
 const time=el('time','—'),clock=el('div',undefined,'task-clock'),clockLabel=el('span','执行用时');time.setAttribute('aria-label','执行用时');clock.append(clockLabel,time);
 const waitTime=el('small','','task-wait-time');waitTime.hidden=true;waitTime.title='排队和本地调度等待，不计入执行用时';
 const usage=el('div',undefined,'task-usage-breakdown'),metrics=el('div',undefined,'task-metrics');metrics.append(clock,waitTime,usage);const controls=el('div',undefined,'task-controls');
 const card={node,title,status,freshness,time,clockLabel,waitTime,usage,controls,body,job,stamp:job.created_at};
 const toggle=action('⌄',()=>{body.hidden=!body.hidden;toggle.textContent=body.hidden?'›':'⌄';toggle.setAttribute('aria-expanded',String(!body.hidden));if(!body.hidden){renderBody(card,ui);if(card.timeline)refreshStageDetails(card);void loadCard(card,ui,true);}});toggle.className='task-expand';toggle.setAttribute('aria-label','展开或收起任务 '+(job.source_title||'PDF'));toggle.setAttribute('aria-controls',body.id);const open=taskCards.size===0||job.status==='RUNNING';body.hidden=!open;toggle.textContent=open?'⌄':'›';toggle.setAttribute('aria-expanded',String(open));
 if(job.source_revision_id)title.href='#source='+encodeURIComponent(job.source_revision_id);node.dataset.taskId=job.id;heading.append(toggle,name,metrics,controls);node.append(heading,body);return card;
}
async function refreshTaskDrawer(jobs,ui){
 if(streamBusy)return;streamBusy=true;const host=document.getElementById('drawerBody');
 if(!clockTimer)clockTimer=setInterval(()=>{if(!document.hidden)tick();},1000);
 async function append(items){const loading=[];for(const job of taskOrder(items)){
  let card=taskCards.get(job.id);if(!card){card=createCard(job,ui);taskCards.set(job.id,card);}card.job=job;const archived=archivedTask(job),parent=archived?archivedHost:host;
  if(card.node.parentElement!==parent){const next=[...parent.children].find(n=>n.dataset.taskId&&taskOrder([{id:n.dataset.taskId,created_at:taskCards.get(n.dataset.taskId).stamp},job])[0].id===job.id);parent.insertBefore(card.node,next||(archived?null:archivedGroup));}
  if(archived){card.title.removeAttribute('href');card.controls.replaceChildren();card.status.textContent='历史任务';}
  if(!archived||archivedGroup.open)loading.push(card);
 }if(document.getElementById('job-drawer').classList.contains('collapsed'))return;let index=0;await Promise.all(Array.from({length:Math.min(3,loading.length)},async()=>{while(index<loading.length)await loadCard(loading[index++],ui);}));}
 try{
  if(!historyButton){historyButton=ui.action('加载更早的任务',async()=>{if(!historyCursor)return;historyButton.disabled=true;try{const older=await progressRequest('/generation-tasks?cursor='+encodeURIComponent(historyCursor));await append(older.items);historyCursor=older.next_cursor;historyButton.hidden=!historyCursor;}finally{historyButton.disabled=false;}});archivedGroup=ui.el('details',undefined,'archived-generation-tasks');archivedSummary=ui.el('summary');archivedHost=ui.el('div');archivedGroup.append(archivedSummary,ui.el('p','保留已移出资料和旧版本的运行记录，便于核对耗时与用量；恢复资料不会重新执行这些任务。'),archivedHost);archivedGroup.addEventListener('toggle',()=>{if(archivedGroup.open)for(const card of taskCards.values())if(archivedTask(card.job))void loadCard(card,ui,true);});host.replaceChildren(archivedGroup,historyButton);}
  const page=await progressRequest('/generation-tasks');await append(page.items);if(historyCursor===null)historyCursor=page.next_cursor;historyButton.hidden=!historyCursor;
  const current=[...taskCards.values()].filter(c=>!archivedTask(c.job)),running=current.filter(c=>['RUNNING','PENDING','RETRY'].includes(c.timeline?.job.status||c.job.status)).length;
  const archived=taskCards.size-current.length;archivedGroup.hidden=!archived;archivedSummary.textContent='已移出或旧版本任务（'+archived+'）';
  document.getElementById('drawerSummary').textContent=current.length?current.length+'个任务'+(running?'，'+running+'个进行中':''):'暂无当前任务';document.getElementById('jobPulse').classList.toggle('running',running>0);
 }catch(e){document.getElementById('drawerSummary').textContent='任务读取失败：'+e.message;}finally{streamBusy=false;}
}
