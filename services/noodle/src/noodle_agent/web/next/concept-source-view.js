export async function appendConceptSources({note,api,el,action,host,openNote,openSource,alive}){
 const data=await api('/notes/'+note.id+'/sources?revision='+encodeURIComponent(note.revision.id));if(!alive())return;
 if(!data.groups.length){host.append(el('p','当前版本没有可展示的来源记录。','muted'));return;}
 for(const group of data.groups){const section=el('section',undefined,'concept-source-group'),header=el('header'),foundation=group.atomics.some(n=>n.role==='FOUNDATION'),supplement=group.atomics.some(n=>n.role==='SUPPLEMENT');header.append(el('h3',group.title),el('span',foundation?'基础来源':supplement?'补充来源':'来源资料','source-role-label'));const pages=group.atomics.flatMap(n=>n.pages),first=pages.length?Math.min(...pages):1;
  if(group.kind==='PDF'){const original=action('查看原文',()=>openSource(group.source_revision_id,{pdf:true,page:first,returnNoteId:note.id}));original.disabled=!group.available;header.append(original);}section.append(header);
  for(const n of group.atomics){const row=el('div',undefined,'concept-source-entry');row.append(action(n.title,()=>openNote(n.note_id,{revision_id:n.revision_id,section_id:n.section_ids[0],returnNoteId:note.id})));const purpose=n.purposes.includes('SUPPORT')?'知识支持':'深入阅读',range=n.pages.length?(n.pages.length>1?`第 ${n.pages[0]}–${n.pages.at(-1)} 页`:`第 ${n.pages[0]} 页`):'';row.append(el('small',[purpose,range,n.section_ids.length+' 个相关小节'].filter(Boolean).join(' · ')));if(!group.available)row.append(el('small','原资料已移出，可查阅保留的历史笔记。'));section.append(row);}host.append(section);
 }
}
