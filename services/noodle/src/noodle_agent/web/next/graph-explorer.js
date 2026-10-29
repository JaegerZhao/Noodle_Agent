import {graphView} from './g6-view.js';
export function graphExplorer({api,el,action,rootId='all',centerId=null,mode='semantic',openNote,openSource,isCurrent=()=>true}){
 const main=el('section',undefined,'unified-graph'),controls=el('div',undefined,'graph-query-controls'),search=el('input');search.type='search';search.placeholder='查找当前范围的节点';search.setAttribute('aria-label','查找图节点');const form=el('form');form.append(search,action('查找',()=>load(0)));controls.append(form);let alive=true,epoch=0,view=null,loaded=null,hops=1,controller=null,query='',includeExternal=Boolean(centerId);
 if(rootId!=='all'){const label=el('label'),check=el('input');check.type='checkbox';check.checked=includeExternal;check.onchange=()=>{includeExternal=check.checked;void load(0);};label.append(check,el('span','包含体系外关联'));controls.append(label);}
 const host=el('div');main.append(controls,host);
 if(centerId)controls.append(action('展开到二跳',()=>{hops=2;return load(0);}),action('回到一跳',()=>{hops=1;return load(0);}));
 const current=t=>alive&&isCurrent()&&t===epoch;
 async function load(offset=0){const ticket=++epoch;controller?.abort();controller=new AbortController();query=search.value.trim();host.setAttribute('aria-busy','true');
  try{const params=new URLSearchParams({root:rootId,mode,limit:150,offset,hops,include_external:includeExternal});if(centerId)params.set('center',centerId);if(offset&&loaded?.snapshot)params.set('snapshot',loaded.snapshot);if(query)params.set('q',query);
   const page=await api('/graph?'+params,{signal:controller.signal});if(!current(ticket))return;const old=loaded;
   if(offset&&old){const nodes=new Map([...old.nodes,...page.nodes].map(n=>[n.id,n])),edges=new Map([...old.edges,...(old.boundary_edges||[]),...page.edges,...(page.boundary_edges||[])].map(e=>[e.edge_id,e]));loaded={...page,nodes:[...nodes.values()],edges:[...edges.values()].filter(e=>nodes.has(e.source)&&nodes.has(e.target))};}else loaded=page;
   view?.dispose();view=graphView(loaded,{el,action,openNote,openSource,selected:centerId,mode,stateKey:[rootId,centerId,mode,hops,query].join('|')});host.replaceChildren(view);if(page.next_offset!=null)host.append(action('加载更多节点',()=>load(page.next_offset)));if(page.boundary_nodes?.length)host.append(el('p',`当前页外还有 ${page.boundary_nodes.length} 个相邻节点，可加载更多查看。`,'muted'));
  }catch(error){if(current(ticket)&&error.name!=='AbortError')host.replaceChildren(el('p','关系读取失败：'+error.message,'status-error'),action('重试',()=>load(0)));}finally{if(current(ticket))host.setAttribute('aria-busy','false');}}
 form.onsubmit=e=>{e.preventDefault();void load(0);};main.ready=load();main.dispose=()=>{alive=false;epoch++;controller?.abort();view?.dispose();};return main;
}
