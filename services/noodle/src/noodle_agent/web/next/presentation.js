import {renderTaskDrawer} from './task-panel.js';
export {startTaskPolling} from './task-panel.js';
import {mountPdfReader} from './pdf-reader.js';
// Classic shell and view composition. No knowledge writes or alternate data models.
const $ = id => document.getElementById(id);
function fitPanels(open='library') {
 const app=$('app');
 if(matchMedia('(min-width:861px) and (max-width:1180px)').matches&&!app.classList.contains('library-collapsed')&&!app.classList.contains('agent-collapsed'))app.classList.add(open==='agent'?'library-collapsed':'agent-collapsed');
}
export function focusPanel(panel = 'reader') {
 const app = $('app');
 if(panel==='agent') { app.classList.remove('agent-collapsed');localStorage.setItem('noodle.next.agentCollapsed','false'); }
 fitPanels(panel==='agent'?'agent':'library');
 if(matchMedia('(max-width: 860px)').matches){app.classList.toggle('mobile-library',panel==='library');app.classList.toggle('mobile-agent',panel==='agent');}
 for(const button of document.querySelectorAll('[data-panel]'))button.classList.toggle('active',button.dataset.panel===panel);
}
export function outlineTab(enabled){$('context').hidden=!enabled;$('noteLibrary').hidden=enabled;for(const [id,on] of [['outlineTab',enabled],['listTab',!enabled]]){$(id).classList.toggle('active',on);$(id).setAttribute('aria-pressed',String(on));}}
export function bindShell({refresh,showTasks}) {
 const app=$('app');
 for(const [key,cls,collapse,expand] of [['library','library-collapsed','collapseLibrary','expandLibrary'],['agent','agent-collapsed','collapseAgent','expandAgent']]){
  app.classList.toggle(cls,localStorage.getItem('noodle.next.'+key+'Collapsed')==='true');
  $(collapse).onclick=()=>{app.classList.add(cls);localStorage.setItem('noodle.next.'+key+'Collapsed','true');};
  $(expand).onclick=()=>{app.classList.remove(cls);fitPanels(key);localStorage.setItem('noodle.next.'+key+'Collapsed','false');};
 }
 fitPanels();
 $('listTab').onclick=()=>outlineTab(false);$('outlineTab').onclick=()=>outlineTab(true);
 $('drawerToggle').onclick=()=>{const open=$('job-drawer').classList.toggle('collapsed')===false;$('drawerToggle').setAttribute('aria-expanded',String(open));$('drawerArrow').textContent=open?'⌄':'⌃';if(open)void showTasks();};
 $('refreshLibrary').onclick=()=>Promise.resolve(refresh()).catch(()=>{});
 for(const button of document.querySelectorAll('[data-panel]'))button.onclick=()=>button.dataset.panel==='jobs'?showTasks():focusPanel(button.dataset.panel);
}
export function viewHeader(el,action,title,summary,upload,refresh){
 const header=el('header',undefined,'view-header'),copy=el('div');copy.append(el('h1',title),el('p',summary,'view-summary'));header.append(copy);
 const tools=el('div',undefined,'header-actions');if(refresh){const b=action('刷新',refresh);b.className='secondary-button';tools.append(b);}if(upload){const b=action('上传 PDF',upload);b.className='primary-button';tools.append(b);}header.append(tools);return header;
}
export function phase(job){const p=JSON.parse(job.details_json||'{}');const stage=job.stage||'';const name=stage==='user-paused'?'已按要求暂停':stage==='book-plan-ready'?'全书规划通过':stage.startsWith('plan-book.')?'规划整本教材':stage==='visual'?'识别图表':stage==='visual-retry'?'图表服务重试':stage==='published'?'生成笔记':stage==='parse'?'解析 PDF':stage==='parse-part'?'分卷解析':stage==='chapter-completed'?'章节完成':stage==='chapter-reused'?'复用已生成章节':stage==='chapter-plan-ready'?'本章规划完成':stage.startsWith('allocate')?'规划主题':stage.startsWith('merge')?'整理主题':stage.startsWith('judge')?'核查内容':stage.startsWith('write')?'撰写笔记':'等待处理';return (p.chapter?p.chapter+' · ':'')+name+(p.total?` · ${p.completed??p.count??0} / ${p.total}`:'');}
export function isUserPaused(job){return (job.status==='PAUSED'||(job.status==='REVIEW'&&JSON.parse(job.error_json||'{}').code==='USER_PAUSED'));}
export function renderDrawer(jobs,ui){return renderTaskDrawer(jobs,ui);}

export function attachPdfReader(reader,toolbar,note,{api,el,action,notice,isCurrent,sourceReader=null,initialPdf=false,initialPage=1}){
 const provenance=note.sections.flatMap(s=>s.provenance||[]).find(p=>p.page);
 const revision=(note.creation_key||'').startsWith('source-map:')?note.creation_key.split(':')[1]:provenance?.source_revision_id;if(!revision)return;
 const controls=el('div',undefined,'content-mode-switch');controls.setAttribute('aria-label','阅读内容');const pane=el('section',undefined,'pdf-content-pane next-pdf-pane');pane.hidden=true;let viewer=null,loading=false,notesScroll=0,lastReadingPage=1;
 const notesMemory=new Map(),pdfMemory=new Map();let selectedLocation=null;
 const content=document.getElementById('content');let mode='notes',pdfScroll=0;
 const show=pdf=>{if(mode===(pdf?'pdf':'notes'))return;if(mode==='notes')notesScroll=content.scrollTop;else pdfScroll=content.scrollTop;mode=pdf?'pdf':'notes';sourceReader?.setMode?.(mode);for(const node of reader.children)if(node!==toolbar)node.hidden=node===pane?!pdf:pdf;for(const [button,active] of [[notes,!pdf],[original,pdf]]){button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));}const top=pdf?pdfScroll:notesScroll;content.scrollTo({top,behavior:'instant'});};
 const notes=action('笔记',async()=>{if(mode==='notes')return;if(selectedLocation&&viewer)pdfMemory.set(selectedLocation.key,viewer.getLocation());const location=selectedLocation;show(false);if(location){const saved=notesMemory.get(location.key);await sourceReader.revealLocation?.(location.key,saved?.offset||0);}});notes.className='active';notes.setAttribute('aria-pressed','true');
 async function openPdf(page,explicit=false){if(loading)return;loading=true;original.disabled=true;try{const destination=!explicit&&selectedLocation?pdfMemory.get(selectedLocation.key)||{page,ratio:0}:{page,ratio:0};if(!viewer){const source=await api('/source-revisions/'+encodeURIComponent(revision));if(!isCurrent())return;const url='/internal/next/sources/'+encodeURIComponent(source.source_id)+'/file?revision='+encodeURIComponent(revision);reader.append(pane);show(true);viewer=mountPdfReader(pane,{url,page,el,action,onLocation(number){if(mode!=='pdf')return;const mapped=sourceReader?.locationForPage?.(number);if(!selectedLocation||!(selectedLocation.pages||[selectedLocation.page]).includes(number))selectedLocation=mapped||selectedLocation;sourceReader?.highlightLocation?.(selectedLocation?.key);}});viewer.restoreLocation(destination);}else{show(true);viewer.restoreLocation(destination);}}catch(error){notice(error.message,true);}finally{loading=false;original.disabled=false;}}
 const original=action('原始 PDF',()=>{if(mode==='pdf')return;selectedLocation=sourceReader?.getLocation?.()||null;if(selectedLocation)notesMemory.set(selectedLocation.key,{offset:sourceReader.relativeOffset?.(selectedLocation)||0});lastReadingPage=sourceReader?.getPage()||provenance?.page||1;return openPdf(lastReadingPage);});original.setAttribute('aria-pressed','false');controls.append(notes,original);toolbar.append(controls);
 sourceReader?.setPdfNavigator((page,open=false,location=null)=>{if(pane.hidden&&!open)return false;if(viewer&&selectedLocation)pdfMemory.set(selectedLocation.key,viewer.getLocation());selectedLocation=location||sourceReader.locationForPage?.(page);if(selectedLocation)notesMemory.delete(selectedLocation.key);if(pane.hidden)void openPdf(page,true);else viewer?.goTo(page);return true;});
 if(initialPdf){selectedLocation=sourceReader?.locationForPage?.(initialPage);void openPdf(initialPage,true);}
}
