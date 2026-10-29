import {knowledgeSystemsView} from './knowledge-systems-view.js';
import {graphExplorer} from './graph-explorer.js';
import {renderScopeSidebar} from './scope-sidebar.js';
import {conceptReader} from './concept-reader.js';
import {workspace,updateScope,scopedRoute,readScope,rememberPosition} from './workspace-state.js';
import {sourceLibraryView,materialLabel} from './source-library-view.js';
import {createSourceImporter} from './source-import.js';
import {createNotice} from './notice.js';
import {composeSourceReader,sourceReadingEntries,readingFingerprint} from './source-reader.js';
import {workspaceApi as api} from './request.js';
import {notesLabel} from './pipeline-status.js';
import {createProgressPolling} from './task-polling.js';
import {clearPdfViewer} from './pdf-reader.js';
import { codePointSlice } from './dialogue.js';
import {createAgentPanel} from './agent-panel.js';
import { renderMarkdown } from './markdown.js';
import { mapEditor, relationEditor } from './organize.js';
import { bindShell, focusPanel, outlineTab, renderDrawer, startTaskPolling, attachPdfReader } from './presentation.js';
const API = '/internal/next', $ = id => document.getElementById(id);
const labels = { ATOMIC: '原子笔记', CONCEPT: '知识点', MOC: '目录与地图', PDF: 'PDF 资料', AUTHOR: '作者修订' };
let agentPanel=null,agentSessionId=null,agentHarnessUrl='http://127.0.0.1:3080/';
function updateAgentLinks(){for(const id of ['agentWorkspace','advancedHarness']){const link=document.getElementById(id);if(!link)continue;const url=new URL(agentHarnessUrl);if(agentSessionId)url.searchParams.set('noodleSession',agentSessionId);link.href=url.href;}}
let selected = null, editing = null, view = 'browse', busy = false, navigationToken = 0, listToken = 0, chatPanel = null;
let refreshReading=null,disposeReading=null,disposeView=null,shellRefreshing=false;
let filesFolder=null;
const sourceStates=new Map();
function el(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined)
    n.textContent = text; if (cls)
    n.className = cls; return n; }
const notice=createNotice({host:$('notice'),el});
function action(label, fn) { const b = el('button', label); b.type = 'button'; b.addEventListener('click', () => Promise.resolve().then(fn).catch(e => notice(e.message, true))); return b; }
function confirmSourceChange(title,message,acceptLabel){
 return new Promise(resolve=>{
  const previous=document.activeElement,dialog=el('dialog',undefined,'source-action-dialog'),heading=el('h2',title),description=el('p',message),buttons=el('div',undefined,'source-action-buttons');
  heading.id='sourceActionTitle';description.id='sourceActionDescription';dialog.setAttribute('aria-labelledby',heading.id);dialog.setAttribute('aria-describedby',description.id);
  let finished=false;const finish=accepted=>{if(finished)return;finished=true;dialog.close();dialog.remove();previous?.focus();resolve(accepted);};
  const cancel=action('取消',()=>finish(false)),accept=action(acceptLabel,()=>finish(true));buttons.append(cancel,accept);dialog.append(heading,description,buttons);dialog.addEventListener('cancel',event=>{event.preventDefault();finish(false);});document.body.append(dialog);dialog.showModal();cancel.focus();
 });
}
function md(target, text, owner = null) { renderMarkdown(target, text,{imageSizes:owner?.display_image_sizes||{},imageSource:raw=>{const match=raw.match(/^(?:\.\.\/)?assets\/([a-f0-9]{64}\.(?:png|jpg|jpeg|webp))$/);return match?API+'/assets/'+match[1]:null;}}); if (owner) linkify(target, owner); for (const a of target.querySelectorAll('a')) {
    const raw = a.getAttribute('href') || '';
    if (raw.startsWith('javascript:'))
        a.removeAttribute('href');
    if (/^https?:/.test(raw)) {
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
    }
} }
function setView(name) { refreshReading=null;disposeReading?.();disposeReading=null;disposeView?.();disposeView=null;clearPdfViewer();navigationToken++; view=name; focusPanel('reader');if(name!=='browse')outlineTab(false); for(const id of ['browse','files','maps','concepts']){$(id).setAttribute('aria-pressed',String(id===name));$(id).classList.toggle('active',id===name);} $('context').replaceChildren(el('h2','阅读导航'));const shell=el('section',undefined,'workbench-view');shell.setAttribute('aria-busy','true');shell.append(el('h1',({files:'全部资料',maps:'知识体系',concepts:'知识点'})[name]||'正在打开笔记'),el('p','正在加载…','muted'));$('content').replaceChildren(shell);$('content').scrollTop=0; }
async function loadList({fresh=false}={}){
 const ticket=++listToken;
 return renderScopeSidebar({api:fresh?path=>api(path,{cache:'no-store',signal:AbortSignal.timeout(12000)}):api,el,action,openSource,openNote,selected,host:$('list'),count:$('count'),query:$('find').value,isCurrent:()=>ticket===listToken});
}
async function openNote(identity, options = {}) {
    setView('browse');
    const ticket=navigationToken;
    const n = await api('/notes/' + encodeURIComponent(identity)+(options.revision_id?'/revisions/'+encodeURIComponent(options.revision_id):''));
    if(ticket!==navigationToken)return;
    if((n.creation_key||'').startsWith('source-map:'))return openSource(n.creation_key.split(':')[1],options);
    selected = n;
    if(n.kind==='CONCEPT'){
      if(workspace.kind!=='KNOWLEDGE')updateScope({kind:'KNOWLEDGE',rootId:'all'});
      outlineTab(true);showAsk(null,false);
      const reader=await conceptReader({note:n,api,el,action,md,host:$('content'),context:$('context'),openNote,openSource,showGraph,
        back:()=>showCatalogue('MOC',{root:workspace.rootId}),editLinks:note=>relationEditor({api,el,action,note,done:openNote,notice}),
        isCurrent:()=>ticket===navigationToken,options});
      if(ticket!==navigationToken){reader.dispose();return;}disposeReading=()=>reader.dispose();
      writeRoute('note='+identity+(options.revision_id?'&revision='+options.revision_id:''));void loadList();return;
    }
    const isSource=(n.creation_key||'').startsWith('source-map:');
    const main = el('article', undefined, 'reader');
    const top=el('header',undefined,'reader-topbar');top.append(action(options.returnToKnowledge?'← 返回知识体系':'← 返回工作台',options.returnToKnowledge?()=>showCatalogue('MOC'):showSources),el('strong',n.title),action(n.kind==='MOC'?'询问知识库':'询问这篇笔记',async()=>{if(n.kind==='MOC'&&(n.creation_key||'').startsWith('source-map:')){const source=await api('/source-revisions/'+n.creation_key.split(':')[1]);showAsk(source.source_id);}else{showAsk();if(n.kind!=='MOC')agentPanel.selectNote({id:n.id,title:n.title});}}));const exportLink=el('a',isSource?'导出完整资料笔记':'导出笔记');exportLink.href=API+'/notes/'+n.id+'/export';exportLink.className='article-export';top.append(exportLink);main.append(top);
    main.append(el('span', isSource?'资料笔记':labels[n.kind], 'tag'), el('h1', n.display_title||n.title));
    if(options.returnNoteId)top.querySelector('button')?.replaceWith(action('返回知识点',()=>openNote(options.returnNoteId,{returnToKnowledge:true})));
    showAsk(null,false);
    const tools = el('div', undefined, 'reader-tools');
    tools.append(action('查看关联', () => showGraph(n.id)), action(isSource?'刷新目录':n.kind === 'MOC' ? '编辑地图' : '管理链接', async () => { if(isSource){await openNote(n.id);return;} if (n.kind === 'MOC')
        await mapEditor({ api, el, action, note: n, done: openNote, notice });
    else
        await relationEditor({ api, el, action, note: n, done: openNote, notice }); }));
    main.append(tools);
    let sourceReader=null;
    if(isSource){
        main.classList.add('source-reader');
        $('content').replaceChildren(main);$('content').scrollTop=0;outlineTab(true);
        sourceReader=await composeSourceReader(main,n,{api,el,action,md,context:$('context'),content:$('content'),editSection,openNote,isCurrent:()=>ticket===navigationToken});
        if(ticket!==navigationToken)return;
    }
    const body = el('div', undefined, 'reading');
    const content = (n.display_article_markdown||n.display_markdown||n.markdown).replace(/^# [^\n]*\n\n/, '');
    if(!isSource){md(body, content, n);main.append(body);}
    if (n.kind === 'MOC'&&!isSource) { // Map links use current note identities, never stale filesystem URLs.
        const tree = el('section');
        tree.append(el('h2', '阅读路线'));
        const entries = n.map_entries;
        function branch(parent) { const ul = el('ul', undefined, 'map-tree'); for (const e of entries.filter(x => x.parent_entry_id === parent)) {
            const li = el('li');
            if (e.target_note_id && e.available === false) li.append(el('span', e.title + '（待更新）', 'muted'));
                else li.append(e.target_note_id ? action(e.title, () => openNote(e.target_note_id)) : el('strong', e.title));
            li.append(branch(e.entry_id));
            ul.append(li);
        } return ul; }
        tree.append(branch(null));
        const headings = [...body.querySelectorAll('h2')];
        const route = headings.find(h => h.textContent === '阅读路线');
        if (route) {
            let next = route.nextSibling;
            while (next) {
                const after = next.nextSibling;
                next.remove();
                next = after;
            }
            route.remove();
        }
        main.append(tree);
    }
    const headings = [...body.querySelectorAll('h2')];
    let headingIndex=0;const omittedHeadings=new Set(n.display_article_omitted_section_headings||[]);
    for (let i = 0; !isSource && i < n.sections.length; i++) {
        const s = n.sections[i], heading = omittedHeadings.has(s.section_id)?main.querySelector('h1'):headings[headingIndex++];
        if (!heading)
            continue;
        heading.id = 'section-' + s.section_id;
        if (n.kind === 'MOC' && n.map_policy !== 'MANUAL')
            continue;
        $('context').append(Object.assign(el('a', s.title), { href: '#' + heading.id }));
    }
    const evidence = new Map();
    for (const s of n.sections)
        for (const p of s.provenance || [])
            evidence.set(p.source_revision_id + ':' + p.page, p);
    if (evidence.size) {
        $('context').append(el('h2', '内容来源'));
        const bySource = new Map();
        for (const p of evidence.values()) {
            if (!bySource.has(p.source_revision_id))
                bySource.set(p.source_revision_id, []);
            if (p.page)
                bySource.get(p.source_revision_id).push(p.page);
        }
        for (const [revision, pages] of bySource) {
            $('context').append(action(pages.length ? '原资料 · 第 ' + [...new Set(pages)].sort((a, b) => a - b).join('、') + ' 页' : '作者修订', async () => { const r = await api('/source-revisions/' + revision); window.open(API + '/sources/' + r.source_id + '/file' + (pages[0] ? '#page=' + pages[0] : ''), '_blank', 'noopener'); }));
        }
    }
    attachPdfReader(main,top,n,{api,el,action,notice,isCurrent:()=>ticket===navigationToken,sourceReader,initialPdf:options.pdf===true});
    if(!isSource){$('content').replaceChildren(main);$('content').scrollTop=0;}
    writeRoute('note='+identity+(options.revision_id?'&revision='+options.revision_id:'')+(options.returnNoteId?'&returnNote='+options.returnNoteId:''));
    for(const button of $('list').querySelectorAll('[data-id]'))button.classList.toggle('selected',button.dataset.id===identity);
    // Backlinks are supplementary; the article is already visible and usable.
    await new Promise(resolve=>requestAnimationFrame(resolve));if(ticket!==navigationToken)return;
    const g = await api('/notes/'+identity+'/connections');
    if(ticket!==navigationToken)return;
    const nodes=new Map(g.nodes.map(n=>[n.id,n]));
    for(const [layer,title]of [['DOCUMENT_LINK','正文引用与反链'],['SUPPORT','来源笔记'],['SEMANTIC','知识关系'],['MEMBERSHIP','收录体系']]){
      const edges=g.edges.filter(e=>e.layer===layer&&(e.source===identity||e.target===identity));
      if(!edges.length)continue;
      const box=el('section',undefined,'note-connections');box.append(el('h2',title));const seen=new Set();
      for(const edge of edges){const outgoing=edge.source===identity,target=nodes.get(outgoing?edge.target:edge.source);if(!target)continue;
        const section=outgoing?edge.target_section_id:edge.from_section_id,anchor=outgoing?edge.target_anchor:null,key=[target.id,section,anchor,edge.relation].join('|');if(seen.has(key))continue;seen.add(key);
        const row=el('p');row.append(action(target.title,()=>openNote(target.id,{section_id:section,anchor,returnToKnowledge:options.returnToKnowledge})));
        row.append(el('small',edge.navigation_role?(outgoing?' 下级主题':' 上级主题'):edge.relation==='USES'?(outgoing?' 使用':' 被使用'):outgoing?' 出链':' 入链'));box.append(row);
      }$('context').append(box);
    }
    if(options.section_id||options.anchor){const heading=options.section_id?main.querySelector('#section-'+CSS.escape(options.section_id)):[...main.querySelectorAll('h2,h3,h4,h5')].find(h=>h.textContent.trim()===options.anchor);if(heading)$('content').scrollTop+=heading.getBoundingClientRect().top-$('content').getBoundingClientRect().top-24;}
}
async function openSource(revision,options={}){
 updateScope({kind:'SOURCES'});
 refreshReading=null;
 setView('browse');const ticket=navigationToken;const source=await api('/source-revisions/'+encodeURIComponent(revision));if(ticket!==navigationToken)return;
 const n={id:source.source_id,kind:'MOC',title:source.title||source.original_name||'资料笔记',creation_key:'source-map:'+revision+':live',sections:[],chapter_key:options.chapterKey};selected=n;
 const main=el('article',undefined,'reader source-reader'),top=el('header',undefined,'reader-topbar');top.append(action('返回资料',showSources));main.append(top,el('h1','资料笔记'));$('content').replaceChildren(main);$('content').scrollTop=0;outlineTab(true);
 if(options.returnNoteId)top.prepend(action('返回知识点',()=>openNote(options.returnNoteId,{returnToKnowledge:true})));
 if(options.returnCitation)top.prepend(action('返回引用',()=>openCitation(options.returnCitation)));
 const settings={api,el,action,md,context:$('context'),content:$('content'),editSection,openNote,isCurrent:()=>ticket===navigationToken,selectChapter:key=>openSource(revision,{...options,chapterKey:key})};
 let sourceReader=await composeSourceReader(main,n,settings);if(ticket!==navigationToken)return;
 if(options.page)await sourceReader?.revealPage?.(options.page);
 let navigatePdf=null,refreshing=false;
 const readerProxy={setPdfNavigator(fn){navigatePdf=fn;sourceReader?.setPdfNavigator(fn);}};
 for(const method of ['getPage','getLocation','relativeOffset','revealLocation','locationForPage','highlightLocation','setMode'])readerProxy[method]=(...args)=>sourceReader?.[method]?.(...args);
 disposeReading=()=>sourceReader?.dispose?.();
 attachPdfReader(main,top,n,{api,el,action,notice,isCurrent:settings.isCurrent,sourceReader:readerProxy,initialPdf:options.pdf===true,initialPage:options.page||1});
 refreshReading=async()=>{
  if(refreshing||!settings.isCurrent())return;refreshing=true;
  try{
   const projection=await sourceReadingEntries(n,api,settings.isCurrent);if(!settings.isCurrent()||readingFingerprint(projection.structure)===sourceReader?.fingerprint)return;
   const readingState=sourceReader?.getReadingState?.();
   const scratch=el('article'),nextContext=el('div'),next=await composeSourceReader(scratch,{...n,chapter_key:readingState?.nodeId||null},{...settings,context:nextContext,projection,outlineState:sourceReader?.getOutlineState?.()});if(!settings.isCurrent()||!next)return;
   next.setOutlineState?.(sourceReader?.getOutlineState?.());
   const container=$('content'),scroll=container.scrollTop,contextScroll=$('context').scrollTop,old=[...main.children].filter(node=>node!==top&&!node.matches('h1,.pdf-content-pane')),hidden=main.querySelector('.source-reading')?.hidden||false;
   const anchor=[...main.querySelectorAll('.source-atomic')].find(node=>node.getBoundingClientRect().bottom>container.getBoundingClientRect().top),anchorId=anchor?.id,anchorTop=anchor?.getBoundingClientRect().top;
   for(const node of [...scratch.children]){node.hidden=hidden;main.insertBefore(node,old[0]||main.querySelector('.pdf-content-pane'));}for(const node of old)node.remove();
   main.querySelector('h1').textContent=n.title;$('context').replaceChildren(...nextContext.childNodes);$('context').scrollTop=contextScroll;sourceReader?.dispose?.();sourceReader=next;sourceReader.setContext?.($('context'));if(navigatePdf)sourceReader.setPdfNavigator(navigatePdf);sourceReader.setMode?.(hidden?'pdf':'notes');
   const replacement=anchorId?[...main.querySelectorAll('.source-atomic')].find(node=>node.id===anchorId):null;container.scrollTop=!hidden&&replacement?scroll+replacement.getBoundingClientRect().top-anchorTop:scroll;
  }finally{refreshing=false;}
 };
 writeRoute('source='+revision+(options.chapterKey?'&chapter='+options.chapterKey:'')+(options.returnNoteId?'&returnNote='+options.returnNoteId:'')+(options.pdf?'&pdf=1&page='+(options.page||1):''));void loadList();showAsk(null,false);
}
function editSection(note, section) { editing = { note, section, revision: note.revision.id }; $('editTitle').value = note.title; $('editBody').value = codePointSlice(note.markdown,section.start_offset,section.end_offset).trim(); $('editError').textContent = ''; $('editor').showModal(); }
$('cancelEdit').onclick = () => $('editor').close();
$('editform').onsubmit = async (e) => { e.preventDefault(); $('saveEdit').disabled = true; try {
    await api('/notes/' + editing.note.id + '/sections', { method: 'PUT', body: JSON.stringify({ expected_revision: editing.revision, section_id: editing.section.section_id, markdown: $('editBody').value, title: $('editTitle').value }) });
    $('editor').close();
    notice('修订已保存，相关知识点和检索索引将随后台任务更新。');
    await openNote(editing.note.id);
}
catch (e) {
    $('editError').textContent = e.message;
}
finally {
    $('saveEdit').disabled = false;
} };
async function showGraph(center=null,initialMode='semantic'){
 setView('browse');outlineTab(false);const ticket=navigationToken;let explorer=null;
 const main=el('section',undefined,'workbench-view'),bar=el('div',undefined,'graph-mode-toolbar'),host=el('div');main.append(action('回到阅读',()=>selected?openNote(selected.id,{returnToKnowledge:true}):showCatalogue('MOC',{root:workspace.rootId})),el('h1','关联内容'),bar,host);$('content').replaceChildren(main);
 function render(mode){explorer?.dispose();for(const b of bar.children)b.classList.toggle('active',b.dataset.mode===mode);explorer=graphExplorer({api,el,action,rootId:workspace.rootId,centerId:center,mode,openNote,openSource,isCurrent:()=>ticket===navigationToken});host.replaceChildren(explorer);writeRoute('view=graph&'+new URLSearchParams({...(center?{center}:{}),graphMode:mode}));}
 for(const [mode,title]of [['semantic','知识链接'],['provenance','来源关系'],['document','正文双链']]){const button=action(title,()=>render(mode));button.dataset.mode=mode;bar.append(button);}disposeView=()=>explorer?.dispose();render(initialMode);void loadList();
}
async function showSources(){
 if(view==='files'&&filesFolder)rememberPosition('files:'+filesFolder,$('content').scrollTop);
 filesFolder=workspace.folder;
 updateScope({kind:'SOURCES'});setView('files');const ticket=navigationToken;
 const main=await sourceLibraryView({api,el,action,openSource,showTasks,sourceStates,notesLabel,confirm:confirmSourceChange,refresh:showSources,notice,isCurrent:()=>ticket===navigationToken,onUpload:()=>$('upload').click()});if(ticket!==navigationToken)return;
 $('content').replaceChildren(main);$('content').scrollTop=workspace.positions['files:'+workspace.folder]||0;writeRoute('view=files');
 void loadList().catch(e=>notice(e.message,true));void sourcePolling.wake();
}
async function showCatalogue(kind='MOC',options={}){
 const initialRoot=kind==='CONCEPT'?'all':options.overview?null:options.root;
 updateScope({kind:'KNOWLEDGE',...(initialRoot?{rootId:initialRoot}:{})});setView(kind==='CONCEPT'?'concepts':'maps');const ticket=navigationToken;
 const panel=knowledgeSystemsView({api,el,action,openNote,initialRoot,isCurrent:()=>ticket===navigationToken,onScope:()=>{void loadList();},
  onState:state=>{if(state.root)updateScope({kind:'KNOWLEDGE',rootId:state.root});writeRoute('view='+(kind==='CONCEPT'?'concepts':'maps')+'&'+new URLSearchParams({...state,...(!state.root?{overview:'1'}:{})}));},
  newRoot:()=>mapEditor({api,el,action,knowledgeRoot:true,done:()=>showCatalogue('MOC',{overview:true}),notice})});
 disposeView=()=>panel.dispose();$('content').replaceChildren(panel);void loadList();
}
async function showTasks(){focusPanel('reader');$('job-drawer').classList.remove('collapsed');$('drawerToggle').setAttribute('aria-expanded','true');$('drawerArrow').textContent='⌄';await renderDrawer([],{el,action,showTasks});}
async function showExports() { const main = el('article', undefined, 'reader'); main.append(el('h1', '导出与本地编辑'), el('p', '阅读包包含 Markdown、相对图片和链接。完整恢复包另含数据库、原始资料与版本记录，请保存在自己的设备上。')); for (const [label, url] of [['下载 Obsidian 阅读包', '/export'], ['下载标准 Markdown 阅读包', '/export?obsidian=false'], ['下载已公开内容', '/export?public=true&obsidian=false'], ['下载完整恢复包', '/recovery']]) {
    const p = el('p');
    p.append(Object.assign(el('a', label), { href: API + url }));
    main.append(p);
} main.append(el('h2', '本地 Vault'), el('p', '先生成 Vault，在本地编辑章节正文，再检查并导入。文件移动可按 ID 识别；删除本地文件不会删除资料。')); main.append(action('生成或刷新 Vault', async () => { const r = await api('/vault/export', { method: 'POST' }); notice('已生成：' + r.path); }), action('检查本地修改', async () => { const r = await api('/vault/import', { method: 'POST' }); notice(`发现 ${r.edits.length} 处章节修改，${r.renamed.length} 个文件移动，${r.missing.length} 个本地缺失文件。`); }), action('导入本地修改', async () => { const r = await api('/vault/import?apply=true', { method: 'POST' }); notice('已导入 ' + r.published.length + ' 处修订。'); await loadList(); }), el('h2', '发布范围'), el('p', '所有笔记默认私有。当前打开的笔记可单独加入或移出公开阅读包。')); if (selected)
    main.append(el('p', selected.title), action('加入公开阅读包', () => setPublic(true)), action('移出公开阅读包', () => setPublic(false))); $('dataBody').replaceChildren(main);if(!$('dataDialog').open)$('dataDialog').showModal(); }
async function setPublic(value) { await api('/notes/' + selected.id + '/public', { method: 'PUT', body: JSON.stringify({ expected_revision: selected.revision.id, public: value }) }); notice(value ? '已加入公开阅读包。' : '已移出公开阅读包。'); }
function showAsk(sourceId=null,focus=true){
 if(focus)focusPanel('agent');
 if(!agentPanel){agentPanel=createAgentPanel({onCitation:openCitation,onContext:text=>{$('assistantContext').textContent=text;},onSession:id=>{agentSessionId=id;updateAgentLinks();}});chatPanel=agentPanel.element;}
 agentPanel.setNote(selected);$('assistantBody').replaceChildren(chatPanel);
 if(sourceId)agentPanel.ready.then(()=>agentPanel.selectSource(sourceId));
 if(focus)agentPanel.focus();
}

$('filter').onsubmit = e => { e.preventDefault();if(view==='files'){updateScope({query:$('find').value,sourceOffset:0});showSources().catch(e=>notice(e.message,true));}else loadList().catch(e => notice(e.message, true)); };
for (const [id, fn] of [['browse', showSources], ['ask', showAsk], ['tasks', showTasks], ['welcomeTasks', showTasks], ['files', showSources], ['maps',()=>showCatalogue('MOC',{overview:true})], ['concepts',()=>showCatalogue('CONCEPT')], ['exportPanel', showExports]])
    if($(id))$(id).onclick = () => Promise.resolve().then(fn).catch(e => notice(e.message, true));
const importSource=createSourceImporter({api,confirm:confirmSourceChange,onState:(state,file)=>notice(({
 UPLOADING:'正在上传 '+file.name+'…',AWAITING_CONFIRMATION:'等待确认重新生成 '+file.name+'。',
 REGENERATING:'正在建立本轮生成任务…',ACCEPTED:'',DUPLICATE:'',CANCELLED:'已取消重新生成，资料仍保留在已删除资料中。',
})[state]||'')});
$('upload').onchange = async () => { const file = $('upload').files[0]; if (!file)
    return; try {
    const r = await importSource(file);if(r.cancelled)return;
    if(!r.duplicate&&!r.regenerated&&workspace.kind==='SOURCES'&&!['all','unfiled'].includes(workspace.folder))await api('/sources/'+r.source_id+'/metadata',{method:'PATCH',body:JSON.stringify({expected_metadata_version:0,folder_id:workspace.folder})});
    await showSources();
    await showTasks();
}
catch (e) {
    notice(e.message, true);
}
finally {
    $('upload').value = '';
} };
async function refreshSourceStates(ticket=navigationToken){
 const states=await api('/source-statuses',{cache:'no-store',signal:AbortSignal.timeout(12000)});let readabilityChanged=false;for(const state of states.items){if(Boolean(sourceStates.get(state.source_revision_id)?.readable)!==Boolean(state.readable))readabilityChanged=true;sourceStates.set(state.source_revision_id,state);}
 if(ticket!==navigationToken||view!=='files')return;
 const rows=new Map([...document.querySelectorAll('[data-source-revision]')].map(r=>[r.dataset.sourceRevision,r]));
 for(const state of states.items){const row=rows.get(state.source_revision_id);if(!row||row.dataset.deleted==='true')continue;const label=row.querySelector('.status-chip');label.removeAttribute('title');label.textContent=notesLabel(state);label.classList.toggle('status-running',state.generation_status==='RUNNING');label.classList.toggle('status-failed',state.generation_status==='REVIEW');row.querySelector('.file-mark').textContent=materialLabel(state.material_profile?.kind);const tally=label.parentElement.querySelector('small');tally.textContent=state.readable?state.coverage.published+'/'+state.coverage.planned+' 篇；语义索引 '+state.coverage.indexed:'';}
 if(readabilityChanged)void loadList({fresh:true}).catch(()=>{});
}
async function refreshShell(){if(shellRefreshing)return;shellRefreshing=true;try{
 const summary=await api('/workspace/summary');$('mapCount').textContent=String(summary.maps);$('conceptCount').textContent=String(summary.concepts);$('sourceCount').textContent=String(summary.sources);$('connection').replaceChildren(el('i'),document.createTextNode('知识服务已连接'));$('connection').parentElement.classList.add('online');
 }finally{shellRefreshing=false;}}
bindShell({refresh:async()=>{api.invalidate();const refresh=view==='files'?showSources():view==='maps'?showCatalogue('MOC'):view==='concepts'?showCatalogue('CONCEPT'):refreshReading?.();await Promise.all([refreshShell(),loadList(),refresh]);},showTasks:()=>showTasks().catch(e=>notice(e.message,true))});
startTaskPolling({el,action,showTasks});
const sourcePolling=createProgressPolling({refresh:()=>refreshSourceStates(),visible:()=>!document.hidden&&view==='files',expanded:()=>true,onError:()=>{for(const row of document.querySelectorAll('[data-source-revision]')){if(row.dataset.deleted!=='true')row.querySelector('.status-chip').title='状态更新延迟，正在自动重连';}}});
for(const event of ['visibilitychange'])document.addEventListener(event,()=>{void sourcePolling.wake();});
for(const event of ['focus','online'])window.addEventListener(event,()=>{void sourcePolling.wake();});
void sourcePolling.start();
showAsk(null,false);
let handledRoute=null;
function writeRoute(value){const hash='#'+scopedRoute(value);if(location.hash!==hash){const url=location.pathname+location.search+hash;if(location.hash)history.pushState(null,'',url);else history.replaceState(null,'',url);}handledRoute=hash;}
function restoreRoute(){
 const hash=location.hash;if(handledRoute===hash)return;handledRoute=hash;
 const params=new URLSearchParams(hash.slice(1));readScope(params);
 if(params.get('answer')&&params.get('citation'))return api('/agent/citations/'+encodeURIComponent(params.get('answer'))+'/'+encodeURIComponent(params.get('citation'))).then(openCitation);
 const source=params.get('source');if(source)return openSource(source,{chapterKey:params.get('chapter')||undefined,returnNoteId:params.get('returnNote')||undefined,pdf:params.get('pdf')==='1',page:Number(params.get('page'))||undefined});
 const id=params.get('note');if(id)return openNote(id,{revision_id:params.get('revision')||undefined,returnNoteId:params.get('returnNote')||undefined});
 const name=params.get('view');if(name==='maps'||name==='concepts')return showCatalogue(name==='maps'?'MOC':'CONCEPT',{root:params.get('root')||undefined,overview:params.get('overview')==='1'});
 if(name==='graph')return showGraph(params.get('center'),params.get('graphMode')||'semantic');if(name==='tasks')return showSources().then(showTasks);if(name==='exportPanel')return showSources().then(showExports);if(!hash||name==='files')return showSources();
}
for(const event of ['popstate','hashchange'])window.addEventListener(event,()=>Promise.resolve().then(restoreRoute).catch(e=>notice(e.message,true)));
Promise.resolve().then(restoreRoute).catch(e=>notice(e.message,true));
setTimeout(()=>{loadList().catch(()=>{});refreshShell().catch(e=>notice(e.message,true));},1500);
setInterval(()=>{if(document.hidden)return;refreshShell().catch(()=>{});refreshReading?.().catch(()=>{});},10000);

api('/config').then(c=>{agentHarnessUrl=c.harness_url;updateAgentLinks();}).catch(()=>{});

function linkify(target,owner){
 const walker=document.createTreeWalker(target,NodeFilter.SHOW_TEXT);const nodes=[];while(walker.nextNode()){const n=walker.currentNode;if(!n.parentElement?.closest('code,pre,a,.katex,math'))nodes.push(n)}
 const open=async raw=>{try{const r=await api('/resolve-link?'+new URLSearchParams({owner:owner.id,target:raw}));await openNote(r.note_id)}catch(e){notice(e.message,true)}};
 for(const text of nodes){const value=text.nodeValue||'';const matches=[...value.matchAll(/\[\[([^\]\n]+)\]\]/g)];if(!matches.length)continue;const fragment=document.createDocumentFragment();let cursor=0;for(const match of matches){fragment.append(document.createTextNode(value.slice(cursor,match.index)));const [raw,label]=match[1].split('|');const a=el('a',label||raw);a.href='#';a.addEventListener('click',e=>{e.preventDefault();open(raw)});fragment.append(a);cursor=match.index+match[0].length}fragment.append(document.createTextNode(value.slice(cursor)));text.replaceWith(fragment)}
 for(const a of target.querySelectorAll('a')){const raw=a.getAttribute('href')||'';if(/\.md(?:#|$)/.test(raw)&&!/^https?:/.test(raw))a.addEventListener('click',e=>{e.preventDefault();open(raw)})}
}


function linkCitations(target,citations){
 const available=new Map(citations.map(c=>[c.citation_id,c]));const walker=document.createTreeWalker(target,NodeFilter.SHOW_TEXT),nodes=[];while(walker.nextNode()){const node=walker.currentNode;if(!node.parentElement.closest('a,code,pre,.katex,math'))nodes.push(node);}
 for(const node of nodes){const text=node.textContent,matches=[...text.matchAll(/\[(K[0-9]+)\]/g)];if(!matches.length)continue;const fragment=document.createDocumentFragment();let cursor=0;for(const match of matches){fragment.append(document.createTextNode(text.slice(cursor,match.index)));const c=available.get(match[1]);if(c){const a=el('a',match[0],'inline-citation');a.href='#note='+c.note_id;a.setAttribute('aria-label','查看引用 '+c.citation_id);a.onclick=e=>{e.preventDefault();openCitation(c).catch(error=>notice(error.message,true));};fragment.append(a);}else fragment.append(document.createTextNode(match[0]));cursor=match.index+match[0].length;}fragment.append(document.createTextNode(text.slice(cursor)));node.replaceWith(fragment);}
}
async function openCitation(citation){
 setView('browse');const ticket=navigationToken;
 const n=await api('/notes/'+citation.note_id+'/revisions/'+citation.revision_id);if(ticket!==navigationToken)return;
 const article=el('article',undefined,'reader');const bar=el('header',undefined,'reader-topbar');bar.append(action('返回问答',()=>showAsk()),el('strong','引用回查'),action('查看当前版本',()=>openNote(citation.note_id)));article.append(bar,el('span','引用对应版本','tag'),el('h1',n.revision.title),el('p',n.eligible?'这是回答引用的固定版本，下方为实际引用范围。':'这是历史版本，当前内容或来源已变化，不再作为有效检索证据。','muted'));
 const excerpt=el('section',undefined,'citation-excerpt');excerpt.dataset.sha256=citation.sha256;excerpt.append(el('h2','回答引用的段落'));const passage=el('div',undefined,'reading');md(passage,codePointSlice(n.markdown,citation.start_offset,citation.end_offset),n);excerpt.append(passage);article.append(excerpt);
 const sourcePages=new Map();for(const p of citation.provenance||[]){if(!Number.isInteger(p.page)||p.page<1)continue;const pages=sourcePages.get(p.source_revision_id)||[];if(!pages.includes(p.page))pages.push(p.page);sourcePages.set(p.source_revision_id,pages);}
 if(sourcePages.size){const sources=el('section',undefined,'citation-source-links');sources.append(el('h2','原始资料'));for(const [revision,pages] of sourcePages){pages.sort((a,b)=>a-b);const label=pages.length>1?pages[0]+'–'+pages.at(-1):String(pages[0]);const b=action('查看原始 PDF · 第 '+label+' 页',()=>openSource(revision,{pdf:true,page:pages[0],returnCitation:citation}));b.dataset.sourceRevision=revision;sources.append(b);}article.append(sources);}
 const details=el('details',undefined,'full-citation-note');details.append(el('summary','展开完整笔记'));const body=el('div',undefined,'reading');md(body,n.markdown.replace(/^# [^\n]*\n\n/,''),n);details.append(body);article.append(details);$('content').replaceChildren(article);$('content').scrollTop=0;
}

$('uploadButton').onclick=()=>$('upload').click();

$('openData').onclick=()=>showExports().catch(e=>notice(e.message,true));$('closeData').onclick=()=>$('dataDialog').close();
