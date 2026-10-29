import {workspace} from './workspace-state.js';
const expansion=new Map();
export async function renderScopeSidebar({api,el,action,openSource,openNote,selected,host,count,query,isCurrent}){
 host.setAttribute('aria-busy','true');
 const scope=[workspace.kind,workspace.folder,workspace.sort,workspace.rootId,query].join('|');if(host.dataset.scope!==scope){host.dataset.scope=scope;host.replaceChildren(el('p','正在读取当前目录…','muted'));count.textContent='';}
 try{
 if(workspace.kind==='SOURCES'){
  const data=await api('/sources?'+new URLSearchParams({folder:workspace.folder,sort:workspace.sort,q:query,include_deleted:false,limit:500}));if(!isCurrent())return;
  const items=data.items.filter(s=>s.kind==='PDF'&&s.atomic_count);host.replaceChildren();count.textContent=items.length+' 份资料笔记';
  for(const s of items){const b=action(s.display_title,()=>openSource(s.current_revision_id));b.dataset.id=s.id;b.className='reader-file-button';b.classList.toggle('selected',selected?.id===s.id);host.append(b);}
 }else{
  const data=await api('/knowledge/roots/'+encodeURIComponent(workspace.rootId)+'/tree?limit=3000');if(!isCurrent())return;
  count.textContent=data.member_total+' 个知识点';const expanded=expansion.get(workspace.rootId)||new Map();expansion.set(workspace.rootId,expanded);const children=new Map();
  const allEntries=new Map(data.entries.map(e=>[e.entry_id,e])),visible=new Set();for(const e of data.entries){if(!e.concept_id||e.available===false)continue;let cursor=e.entry_id;while(cursor&&!visible.has(cursor)){visible.add(cursor);cursor=allEntries.get(cursor)?.parent_entry_id;}}for(const e of data.entries){if(!visible.has(e.entry_id))continue;if(!children.has(e.parent_entry_id))children.set(e.parent_entry_id,[]);children.get(e.parent_entry_id).push(e);}
  const parents=new Map(data.entries.map(e=>[e.entry_id,e.parent_entry_id])),active=data.entries.find(e=>(e.concept_id||e.target_note_id)===selected?.id),branch=new Set();let at=active?.entry_id;while(at){branch.add(at);at=parents.get(at);}
  function draw(parent,depth){for(const entry of children.get(parent)||[]){const kids=children.get(entry.entry_id)||[],inQuery=!query||entry.title.toLocaleLowerCase().includes(query.toLocaleLowerCase());if(query&&!inQuery){draw(entry.entry_id,depth);continue;}
   const row=el('div',undefined,'scope-tree-row');row.style.paddingLeft=(depth*10)+'px';const opened=expanded.has(entry.entry_id)?expanded.get(entry.entry_id):depth<2||branch.has(entry.entry_id);
   if(kids.length){const arrow=action(opened?'▾':'▸',()=>{expanded.set(entry.entry_id,!opened);render();});arrow.className='scope-tree-arrow';arrow.setAttribute('aria-label',(opened?'收起 ':'展开 ')+entry.title);arrow.setAttribute('aria-expanded',String(opened));row.append(arrow);}else row.append(el('span','', 'scope-tree-spacer'));
   const identity=entry.available===false?null:entry.concept_id,b=action(entry.title+(entry.reference?' ↗':''),()=>identity&&openNote(identity,{returnToKnowledge:true,...(entry.target_mode==='PINNED'&&entry.target_revision_id?{revision_id:entry.target_revision_id}:{})}));b.className='reader-file-button';if(entry.reference)b.title='同一知识点在另一上级下的引用';b.classList.toggle('selected',selected?.id===identity);if(!identity)b.disabled=true;row.append(b);host.append(row);if(opened||query)draw(entry.entry_id,depth+1);}}
  function render(){host.replaceChildren();draw(null,0);}render();
  if(data.truncated)host.append(el('p','当前目录超过首屏范围，请通过体系或搜索缩小范围。','muted'));
 }
 if(!host.children.length)host.append(el('p','当前范围还没有可读笔记。','empty'));host.setAttribute('aria-busy','false');
 }catch(error){if(isCurrent()){count.textContent='';host.replaceChildren(el('p','目录读取失败：'+error.message,'muted'),action('重试目录',()=>isCurrent()&&renderScopeSidebar({api,el,action,openSource,openNote,selected,host,count,query,isCurrent})));}}
 finally{if(isCurrent())host.setAttribute('aria-busy','false');}
}
