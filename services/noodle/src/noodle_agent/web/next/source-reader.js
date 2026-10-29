// A source reading view projects existing revisions; it never publishes another body.
export function sourceEntries(entries){
 const children=new Map();for(const entry of entries){const parent=entry.parent_entry_id||null;if(!children.has(parent))children.set(parent,[]);children.get(parent).push(entry);}
 for(const list of children.values())list.sort((a,b)=>a.position-b.position||a.entry_id.localeCompare(b.entry_id));
 const result=[],visited=new Set();function walk(parent,depth){for(const entry of children.get(parent)||[]){if(visited.has(entry.entry_id))continue;visited.add(entry.entry_id);result.push({...entry,depth});walk(entry.entry_id,depth+1);}}walk(null,0);return result;
}
const noteCaches=new WeakMap();
export async function loadSourceNotes(entries,api,isCurrent,onProgress=()=>{}){
 if(!noteCaches.has(api))noteCaches.set(api,new Map());const cache=noteCaches.get(api);
 const targets=[...new Map(entries.filter(e=>e.target_note_id&&e.available!==false).map(e=>[e.target_note_id,e])).values()],result=new Map();let cursor=0;
 await Promise.all(Array.from({length:Math.min(4,targets.length)},async()=>{while(cursor<targets.length&&isCurrent()){const entry=targets[cursor++],key=entry.target_revision_id&&entry.target_note_id+':'+entry.target_revision_id+':'+(entry.reading_binding||'');try{const note=(key&&cache.get(key))||await api('/notes/'+encodeURIComponent(entry.target_note_id)+(entry.reading_source_revision?'?reading_source_revision='+encodeURIComponent(entry.reading_source_revision):''));if(note.kind!=='ATOMIC'||!note.eligible||(entry.target_revision_id&&entry.target_revision_id!==note.revision.id))throw Error('当前版本不可用');if(key){cache.set(key,note);if(cache.size>256)cache.delete(cache.keys().next().value);}result.set(entry.target_note_id,{note});}catch(error){result.set(entry.target_note_id,{error:error.message});}onProgress(result.size,targets.length);}}));return result;
}
export const headingKey=title=>title.replace(/^\s*(?:\d+(?:\.\d+)*[.、]?\s*|第[一二三四五六七八九十\d]+[章节]\s*)/,'').replace(/[^\p{L}\p{N}]/gu,'').toLocaleLowerCase();
// Match presentation headings within this Atomic's source subtree only. A
// uniquely bracketed one-to-one sibling run can retain source order when the
// writer uses a different label. This never changes publication or evidence.
export function outlineHeadingBindings(headings,nodes,root){
 const byId=new Map(nodes.map(n=>[n.entry_id,n]));
 const within=node=>{let id=node.entry_id;while(id&&byId.has(id)){if(id===root)return true;id=byId.get(id).parent_entry_id;}return false;};
 const candidates=nodes.filter(n=>n.entry_id!==root&&within(n)),bindings=new Map(),used=new Set();
 for(const [i,h] of headings.entries()){const matches=candidates.filter(n=>headingKey(n.title)===headingKey(h.title));if(matches.length===1&&!used.has(matches[0].entry_id)){bindings.set(i,matches[0]);used.add(matches[0].entry_id);}}
 const anchors=[...bindings.entries()];
 for(let k=1;k<anchors.length;k++){
  const [a,left]=anchors[k-1],[b,right]=anchors[k];if(left.parent_entry_id!==right.parent_entry_id||headings[a].level!==headings[b].level)continue;
  const siblings=candidates.filter(n=>n.parent_entry_id===left.parent_entry_id).sort((x,y)=>x.position-y.position),start=siblings.indexOf(left),end=siblings.indexOf(right);
  const gap=siblings.slice(start+1,end),indices=[];for(let i=a+1;i<b;i++)if(headings[i].level===headings[a].level)indices.push(i);
  if(end>start&&gap.length&&gap.length===indices.length&&gap.every(n=>!used.has(n.entry_id))&&indices.every(i=>!bindings.has(i)))for(let i=0;i<gap.length;i++){bindings.set(indices[i],gap[i]);used.add(gap[i].entry_id);}
 }
 return bindings;
}
export function atomicDisplayTitle(title,parentTitle){if(!parentTitle||title.includes('*')&&!parentTitle.includes('*'))return title;return title.split(/[；;]/).filter(part=>headingKey(part)!==headingKey(parentTitle)).join('；');}
export const readingFingerprint=data=>data?.version||JSON.stringify([data?.display_title,data?.outline?.nodes,data?.notes,data?.planned_notes,data?.chapter_tasks,data?.planning_status,data?.generation_status,data?.readiness?.notes_status]);
const projections=new WeakMap();
export async function sourceReadingEntries(note,api,isCurrent){
 const revision=note.creation_key.split(':')[1],path='/source-revisions/'+encodeURIComponent(revision)+'/reading';
 if(!projections.has(api))projections.set(api,new Map());const cache=projections.get(api),previous=cache.get(revision);
 if(previous){const stamp=await api(path+'?view=version');if(!isCurrent())return [];if(stamp.version&&stamp.version===previous.structure.version)return previous;}
 const data=await api(path+'?view=manifest');if(!isCurrent())return [];
 const entries=data.outline.nodes.map(n=>({entry_id:n.node_id,parent_entry_id:n.parent_id,title:n.title,position:n.start*10000+n.position,target_note_id:null,kind:n.kind,status:n.status,published_count:n.published_count,planned_count:n.planned_count,start:n.start,end:n.end}));
 const keys=new Set(data.notes.map(n=>n.creation_key));
 for(const n of data.notes)entries.push({entry_id:'published-'+n.note_id,parent_entry_id:n.primary_node_id,title:n.display_title||n.title,position:(n.pages[0]||999999)*10000+9000,target_note_id:n.note_id,target_revision_id:n.revision_id,section_anchors:n.section_anchors||[],chapter_key:n.chapter_key,reading_source_revision:data.schema_version==='reading-manifest-v1'?revision:null,reading_binding:data.anchor_binding,available:true,kind:'PUBLISHED_NOTE'});
 for(const [i,n] of data.planned_notes.entries())if(!keys.has(n.creation_key))entries.push({entry_id:'planned-'+i,parent_entry_id:n.primary_node_id,title:n.display_title||n.title,position:(n.pages[0]||data.outline.nodes.find(x=>x.node_id===n.primary_node_id)?.start||999999)*10000+9500+i,target_note_id:null,kind:'PLANNED_NOTE',status:n.status});
 entries.structure=data;cache.set(revision,entries);if(cache.size>4)cache.delete(cache.keys().next().value);return entries;
}

export function chapterGroups(entries){
 const structure=entries.structure||{},nodes=new Map(entries.filter(e=>!['PUBLISHED_NOTE','PLANNED_NOTE'].includes(e.kind)).map(e=>[e.entry_id,e]));
 const chapters=(structure.document_profile?.kind==='BOOK'?structure.chapter_tasks||[]:[]).filter(c=>nodes.has(c.chapter_key));
 const ids=new Set(chapters.map(c=>c.chapter_key));
 const owner=id=>{while(id&&nodes.has(id)){if(ids.has(id))return id;id=nodes.get(id).parent_entry_id;}return null;};
 return {chapters,owner,entries:key=>entries.filter(e=>e.target_note_id&&(e.chapter_key===key||owner(e.parent_entry_id)===key))};
}
export async function composeSourceReader(...args){return (await import('./source-reading-view.js')).composeSourceReader(...args);}
