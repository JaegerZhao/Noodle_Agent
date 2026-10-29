// The graph projection normalizes TOPIC_PARENT/CHILD to parent -> child.
export function directFamily(identity,nodes,edges){
 const byId=new Map(nodes.map(n=>[n.id,n])),parents=new Map(),children=new Map();
 for(const edge of edges){if(!edge.navigation_role||edge.status==='SUGGESTED')continue;const parent=edge.target===identity,other=byId.get(parent?edge.source:edge.target);if((edge.source!==identity&&edge.target!==identity)||!other||other.id===identity||other.kind!=='CONCEPT')continue;(parent?parents:children).set(other.id,{id:other.id,title:other.title});}
 const sorted=values=>[...values.values()].sort((a,b)=>a.title.localeCompare(b.title,'zh-CN'));
 return {parents:sorted(parents),children:sorted(children)};
}

export async function appendRelatedConcepts({note,api,el,action,host,openNote,alive}){
 const content=el('div');host.append(content);
 async function load(){content.replaceChildren(el('p','正在读取关联知识…','muted'));try{
  const nodes=new Map(),edges=new Map();let offset=0,snapshot=null;
  do{const params=new URLSearchParams({center:note.id,root:'all',mode:'hierarchy',hops:1,limit:1000,offset});if(snapshot)params.set('snapshot',snapshot);const page=await api('/graph?'+params);if(!alive())return;snapshot=page.snapshot;for(const n of page.nodes)nodes.set(n.id,n);for(const e of [...page.edges,...(page.boundary_edges||[])])edges.set(e.edge_id,e);offset=page.next_offset;}while(offset!=null);
  const family=directFamily(note.id,[...nodes.values()],[...edges.values()]);content.replaceChildren();
  for(const [key,label]of [['parents','父级知识点'],['children','子级知识点']]){const group=el('div',undefined,'related-concept-group');group.append(el('h3',label));if(!family[key].length)group.append(el('p','暂无','muted'));else{const links=el('div',undefined,'related-concept-links');for(const item of family[key])links.append(action(item.title,()=>openNote(item.id,{returnToKnowledge:true})));group.append(links);}content.append(group);}
 }catch(error){if(alive())content.replaceChildren(el('p','关联知识读取失败：'+error.message,'muted'),action('重试',load));}}
 await load();
}
