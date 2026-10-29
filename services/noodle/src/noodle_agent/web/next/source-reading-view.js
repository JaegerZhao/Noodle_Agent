import {sourceEntries,sourceReadingEntries,loadSourceNotes,headingKey,readingFingerprint,outlineHeadingBindings,chapterGroups} from './source-reader.js';
import {scrollMotion} from './reading-motion.js';
import {renderTitle} from './markdown.js';

const manualBySource=new Map();
export const outlineOpen=(key,manual,branch,baseOpen=false)=>manual.has(key)?manual.get(key):baseOpen||branch.has(key);
export const defaultOutlineOpen=depth=>depth<2;
export const sectionOwner=(entry,anchor,binding,byId,isBook)=>isBook?(binding?.entry_id||(byId.has(anchor?.node_id)?anchor.node_id:entry.parent_entry_id)):(anchor?.reading_owner||entry.parent_entry_id);
export const tocScrollTop=(scroll,rowTop,rowHeight,containerTop,height,total)=>Math.max(0,Math.min(total-height,scroll+rowTop+rowHeight/2-containerTop-height*.382));
export function outlineFollow(state){
 let manual=Boolean(state?.manual);
 return {pause(){manual=true;},resume(){const changed=manual;manual=false;return changed;},get paused(){return manual;},snapshot(){return {manual};},restore(value){manual=Boolean(value?.manual);}};
}

// A reading location is a generated section (or an ungenerated source heading).
// Each mode remembers its own offset within that location.
export async function composeSourceReader(main,n,{api,el,action,md,context,content,isCurrent,projection,outlineState}){
 const loading=el('p','正在加载文章目录…','muted source-reading-status');main.append(loading);
 let projected;try{projected=projection||await sourceReadingEntries(n,api,isCurrent);}catch(error){loading.textContent='文章加载失败：'+error.message;throw error;}if(!isCurrent())return null;
 const structure=projected.structure||{},fingerprint=readingFingerprint(structure),revision=n.creation_key.split(':')[1];
 if(!manualBySource.has(revision)){let saved=[];try{saved=JSON.parse(localStorage.getItem('noodle.next.outline.'+revision)||'[]');}catch{}manualBySource.set(revision,new Map(Array.isArray(saved)?saved.filter(v=>Array.isArray(v)&&typeof v[0]==='string'&&typeof v[1]==='boolean'):[]));}
 const manual=manualBySource.get(revision);
 const remember=(key,open)=>{manual.set(key,open);try{localStorage.setItem('noodle.next.outline.'+revision,JSON.stringify([...manual]));}catch{}};
 const body=el('div',undefined,'reading source-reading'),status=loading;let navContext=context;main.append(body);
 context.replaceChildren(el('h2','文章目录'));
 if(structure.display_title){n.title=structure.display_title;const h=main.querySelector('h1');if(h)h.textContent=n.title;}
 if(!structure.counts?.planned&&!structure.counts?.published&&structure.generation_status!=='SUCCEEDED'){
  status.textContent='正在整理中文学习目录…';return {fingerprint,getPage:()=>1,setPdfNavigator(){},dispose(){}};
 }
 const entries=sourceEntries(projected),nodes=entries.filter(e=>!['PUBLISHED_NOTE','PLANNED_NOTE'].includes(e.kind)),byId=new Map(nodes.map(e=>[e.entry_id,e]));
 const chapters=chapterGroups(projected),chapterIds=new Set(chapters.chapters.map(c=>c.chapter_key)),lazy=chapterIds.size>1;
 const loaded=new Map(),processed=new Set(),mounted=new Set(),chapterSlots=new Map(),chapterLoads=new Map(),chapterHeights=new Map(),allLocations=new Map();
 let alive=true,chapterObserver=null,offsets=null,navigationChapter=null,navigationSequence=0;
 const children=new Map(),sectionsByNode=new Map(),pendingByNode=new Map(),locations=[],targets=new Map(),navRows=new Map(),groups=new Map();
 let pdfNavigator=null,pdfMode=false,frame=0,active=null,failures=0,pending=null,focusTimer=0,navigationTarget=null,resumePending=false;
 const follow=outlineFollow(outlineState),outlineInputs=['wheel','pointerdown','touchstart','keydown'];
 const contentMotion=scrollMotion(content);let outlineMotion=scrollMotion(navContext);
 function manualOutline(){follow.pause();resumePending=false;outlineMotion.cancel();}
 function bindContext(value){for(const type of outlineInputs)navContext.removeEventListener(type,manualOutline);outlineMotion.cancel();navContext=value;outlineMotion=scrollMotion(value);for(const type of outlineInputs)navContext.addEventListener(type,manualOutline,{passive:true});}
 bindContext(navContext);
 for(const e of nodes){const parent=byId.has(e.parent_entry_id)?e.parent_entry_id:null;if(!children.has(parent))children.set(parent,[]);children.get(parent).push(e);}
 const firstRoot=children.get(null)?.[0];
 for(const entry of entries.filter(e=>e.kind==='PLANNED_NOTE')){
  const owner=byId.has(entry.parent_entry_id)?entry.parent_entry_id:firstRoot?.entry_id;
  if(!pendingByNode.has(owner))pendingByNode.set(owner,[]);pendingByNode.get(owner).push(entry);
 }
 async function loadEntries(selected){
 const incoming=await loadSourceNotes(selected,api,()=>alive&&isCurrent(),(done,total)=>{if(alive&&isCurrent())status.textContent=`正在加载当前章节 ${done}/${total}…`;});
 if(!alive||!isCurrent())return;
 for(const [key,value] of incoming)loaded.set(key,value);
 for(const entry of selected.filter(e=>e.target_note_id&&!processed.has(e.target_note_id))){
  processed.add(entry.target_note_id);
  const item=loaded.get(entry.target_note_id),note=item?.note;
  if(!note){failures++;const owner=entry.parent_entry_id||firstRoot?.entry_id;if(!sectionsByNode.has(owner))sectionsByNode.set(owner,[]);sectionsByNode.get(owner).push({entry,error:item?.error||'版本不可用'});continue;}
  const rendered=el('div');md(rendered,(note.display_article_markdown||note.display_markdown||note.markdown).replace(/^# [^\n]*\n\n/,''),note);
  const sectionBindings=outlineHeadingBindings(note.sections.map(s=>({title:s.title,level:2})),nodes,entry.parent_entry_id);
  const chunks=[];let chunk=null;
  if(note.display_article_omitted_section_headings?.includes(note.sections[0]?.section_id)){
   chunk={heading:null,nodes:[]};chunks.push(chunk);
  }
  for(const child of [...rendered.childNodes]){
   if(child.nodeType===1&&child.matches('h2')){chunk={heading:child,nodes:[]};chunks.push(chunk);}
   else if(chunk)chunk.nodes.push(child);
  }
  // Section IDs and evidence come from the immutable publication contract.
  for(const [i,s] of note.sections.entries()){
   const anchor=(note.source_section_anchors||entry.section_anchors).find(a=>a.section_id===s.section_id),owner=sectionOwner(entry,anchor,sectionBindings.get(i),byId,structure.document_profile?.kind==='BOOK')||firstRoot?.entry_id;
   const pages=anchor?.pages?.length?anchor.pages:[...new Set((s.provenance||[]).filter(p=>p.source_revision_id===revision&&p.page).map(p=>p.page))].sort((a,b)=>a-b);
   if(!sectionsByNode.has(owner))sectionsByNode.set(owner,[]);
   const chunk=chunks[i],headingNodes=(chunk?.nodes||[]).filter(v=>v.nodeType===1&&/^H[1-6]$/.test(v.tagName));
   const bindings=outlineHeadingBindings(headingNodes.map(v=>({title:v.textContent,level:Number(v.tagName.slice(1))})),nodes,entry.parent_entry_id);
   if(!bindings.size){sectionsByNode.get(owner).push({entry,note,section:s,chunk,pages});continue;}
   let fragment={entry,note,section:s,chunk:{nodes:[]},pages,owner:headingKey(s.title)===headingKey(byId.get(entry.parent_entry_id)?.title||'')?entry.parent_entry_id:owner};
   const fragments=[fragment];
   for(const child of chunk.nodes){const target=bindings.get(headingNodes.indexOf(child));if(target){fragment={entry,note,section:{...s,title:target.title},chunk:{nodes:[]},pages:[target.start||pages[0]],owner:target.entry_id,fragmentKey:target.entry_id};fragments.push(fragment);}else fragment.chunk.nodes.push(child);}
   for(const part of fragments){if(!part.chunk.nodes.some(v=>v.textContent?.trim()||v.nodeType===1&&v.querySelector?.('img')))continue;if(!sectionsByNode.has(part.owner))sectionsByNode.set(part.owner,[]);sectionsByNode.get(part.owner).push(part);}
  }
  // Yield between notes so the outline and loading state remain responsive.
  await new Promise(resolve=>requestAnimationFrame(resolve));
 }
 }
 if(!lazy){await loadEntries(entries);if(!isCurrent())return null;}
 const jump=(target,animate=false)=>{const top=target.getBoundingClientRect().top-content.getBoundingClientRect().top+content.scrollTop-65;contentMotion.to(top,{immediate:!animate||Math.abs(top-content.scrollTop)>content.clientHeight*1.5,onComplete(){navigationTarget=null;navigationChapter=null;sync(true);}});};
 function finishNavigation(){if(navigationTarget){const target=navigationTarget;navigationTarget=null;contentMotion.cancel();jump(target);}}
 const interruptNavigation=()=>{navigationSequence++;navigationTarget=null;navigationChapter=null;contentMotion.cancel();};content.addEventListener('wheel',interruptNavigation,{passive:true});content.addEventListener('touchstart',interruptNavigation,{passive:true});
 function readerInput(event){
  if(event.type==='keydown'&&!['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(event.key))return;
  if(event.type==='pointerdown'&&event.target!==content)return;
  if(event.type==='pointerdown')interruptNavigation();
  if(follow.resume())resumePending=true;
 }
 for(const type of outlineInputs)content.addEventListener(type,readerInput,{passive:true});
 function ancestors(id){const ids=[];while(id&&byId.has(id)){ids.push(id);id=byId.get(id).parent_entry_id;}return ids;}
 function highlight(location,force=false){
  if(!location||!isCurrent()||active===location.key&&!force)return;active=location.key;
  const chain=new Set(ancestors(location.nodeId));
  for(const [key,row] of navRows){const current=key===location.key;row.classList.toggle('active',current);row.classList.toggle('active-ancestor',chain.has(key)&&!current);if(current)row.setAttribute('aria-current','location');else row.removeAttribute('aria-current');}
  if(follow.paused)return;
  for(const [id,group] of groups){const open=outlineOpen(id,manual,chain,group.baseOpen),wasHidden=group.children.hidden;group.children.hidden=!open;group.toggle.setAttribute('aria-expanded',String(open));group.toggle.textContent=open?'⌄':'›';group.toggle.dataset.expansion=manual.has(id)?'manual':'auto';if(open&&wasHidden&&!matchMedia('(prefers-reduced-motion: reduce)').matches)group.children.animate?.([{opacity:.55},{opacity:1}],{duration:140});}
  const row=navRows.get(location.key);if(!row||!navContext.isConnected)return;
  const bounds=navContext.getBoundingClientRect(),box=row.getBoundingClientRect();
  // Scroll only the TOC, never scrollIntoView (which also moves the reader).
  if(row.getClientRects().length)outlineMotion.to(tocScrollTop(navContext.scrollTop,box.top,box.height,bounds.top,navContext.clientHeight,navContext.scrollHeight));
 }
 async function navigate(location){follow.resume();resumePending=false;clearTimeout(focusTimer);if(pdfNavigator?.(location.page,false,location)){highlight(location,true);return;}const sequence=++navigationSequence;navigationChapter=chapters.owner(location.nodeId);await ensureChapter(navigationChapter,true);if(!alive||!isCurrent()||sequence!==navigationSequence)return;location=allLocations.get(location.key)||location;highlight(location,true);navigationTarget=location.target;jump(location.target,true);}
 function rowFor(title,key,parent,location,hasChildren=false){
  const group=el('div',undefined,'source-outline-group'),row=el('div',undefined,'source-outline-row'),label=action(title,()=>navigate(location));renderTitle(label,title);label.className='source-outline-link';label.dataset.locationKey=key;row.append(label);group.append(row);parent.append(group);navRows.set(key,label);
  if(hasChildren){const baseOpen=defaultOutlineOpen(byId.get(key)?.depth??((byId.get(location.nodeId)?.depth??0)+1)),open=outlineOpen(key,manual,new Set(),baseOpen);const nested=el('div',undefined,'source-outline-children'),toggle=action(open?'⌄':'›',()=>{nested.hidden=!nested.hidden;remember(key,!nested.hidden);toggle.setAttribute('aria-expanded',String(!nested.hidden));toggle.textContent=nested.hidden?'›':'⌄';toggle.dataset.expansion='manual';});toggle.className='source-outline-toggle';toggle.setAttribute('aria-label','展开或收起 '+title);toggle.setAttribute('aria-expanded',String(open));nested.hidden=!open;row.prepend(toggle);group.append(nested);groups.set(key,{children:nested,toggle,baseOpen});return nested;}
  row.classList.add('source-outline-leaf');return group;
 }
 function renderNode(node,navParent){
  if(lazy&&chapterIds.has(node.entry_id)&&!mounted.has(node.entry_id))return renderPlaceholder(node,navParent);
  const group=el('section',undefined,'source-topic'),heading=el('h'+Math.min(6,node.depth+2),node.title,node.kind==='SECTION'?'source-subsection-heading':'source-chapter-heading');heading.id='source-entry-'+node.entry_id;group.append(heading);targets.set(node.entry_id,heading);
  const location={key:node.entry_id,nodeId:node.entry_id,page:node.start||1,target:heading,title:node.title};locations.push(location);allLocations.set(location.key,location);
  const own=sectionsByNode.get(node.entry_id)||[],sub=children.get(node.entry_id)||[],pending=pendingByNode.get(node.entry_id)||[];
  const distinct=own.filter(x=>x.section&&headingKey(x.section.title)!==headingKey(node.title));
  const nested=rowFor(node.title,node.entry_id,navParent,location,Boolean(sub.length||distinct.length||pending.length));
  renderTitle(heading,node.title);
  for(const item of own){
   const block=el('section',undefined,'source-atomic');block.dataset.noteId=item.entry.target_note_id;
   if(item.error){block.append(el('p','笔记暂不可用：'+item.error,'status-error'));group.append(block);continue;}
   block.id='source-section-'+item.note.id+'-'+item.section.section_id+(item.fragmentKey?'-'+item.fragmentKey:'');block.dataset.revision=item.note.revision.id;
   const same=headingKey(item.section.title)===headingKey(node.title),sectionHeading=el('h'+Math.min(6,node.depth+3),item.section.title,'source-generated-heading');
   if(!same){renderTitle(sectionHeading,item.section.title);block.append(sectionHeading);}
   const text=el('div',undefined,'source-atomic-body');text.append(...(item.chunk?.nodes||[]));
   for(const h of [...text.querySelectorAll('h1,h2,h3,h4,h5,h6')]){const replacement=el('h'+Math.min(6,node.depth+Number(h.tagName.slice(1))+2));replacement.append(...h.childNodes);h.replaceWith(replacement);}
   block.append(text);group.append(block);
   if(same){location.page=item.pages[0]||location.page;continue;}
   const sectionLocation={key:block.id,nodeId:node.entry_id,page:item.pages[0]||node.start||1,pages:item.pages,target:block,title:item.section.title};locations.push(sectionLocation);allLocations.set(sectionLocation.key,sectionLocation);rowFor(item.section.title,block.id,nested,sectionLocation);
  }
  for(const item of pending){const omitted=item.status==='OMITTED',label=item.title+(omitted?'（无需笔记）':item.status==='DEFERRED'?'（待恢复）':'（待生成）');const hint=el('p',label,'planned-note-placeholder');hint.id='source-entry-'+item.entry_id;group.append(hint);const pendingLocation={key:item.entry_id,nodeId:node.entry_id,page:node.start||1,target:hint,title:item.title};locations.push(pendingLocation);allLocations.set(pendingLocation.key,pendingLocation);rowFor(label,item.entry_id,nested,pendingLocation);}
  for(const child of sub)group.append(renderNode(child,nested));
  if(!own.length&&!sub.length&&!pending.length){
   const chain=ancestors(node.entry_id),waiting=chain.flatMap(id=>pendingByNode.get(id)||[]),shared=node.merged_into&&entries.find(e=>e.target_note_id===node.merged_into);
   const hint=el('p',undefined,'source-section-state');
   if(waiting.length)hint.textContent=waiting.some(e=>e.status==='DEFERRED')?'本节尚未完成，可在生成任务中恢复。':'本节对应的笔记尚未生成。';
   else if(node.status==='NO_NOTES')hint.textContent='此目录项无需独立笔记。';
   else if(shared){hint.append('本小节随合并笔记呈现。',action('查看对应内容',()=>{const target=body.querySelector(`[data-note-id="${shared.target_note_id}"]`);if(target)jump(target,true);}));}
   else if(node.status==='PLANNED'||node.status==='DEFERRED')hint.textContent='本节对应的笔记尚未生成。';
   if(hint.textContent)group.append(hint);
  }
  if(chapterIds.has(node.entry_id))chapterSlots.set(node.entry_id,{node,body:group,nav:navRows.get(node.entry_id).closest('.source-outline-group')});
  return group;
 }
 function outlineOnly(node,parent,target){
  const location={key:node.entry_id,nodeId:node.entry_id,page:node.start||1,target,title:node.title};allLocations.set(location.key,location);
  const sub=children.get(node.entry_id)||[],nested=rowFor(node.title,node.entry_id,parent,location,Boolean(sub.length));
  for(const child of sub)outlineOnly(child,nested,target);
 }
 function renderPlaceholder(node,parent){
  const group=el('section',undefined,'source-topic source-chapter-placeholder'),heading=el('h'+Math.min(6,node.depth+2),node.title,'source-chapter-heading');
  heading.id='source-entry-'+node.entry_id;group.dataset.chapterKey=node.entry_id;group.append(heading,el('p','本章按需加载','muted'),action('加载本章',()=>navigate(allLocations.get(node.entry_id))));
  renderTitle(heading,node.title);
  const pages=chapters.entries(node.entry_id).flatMap(e=>structure.notes.find(n=>n.note_id===e.target_note_id)?.pages||[]);
  const estimate=pages.length?(Math.max(...pages)-Math.min(...pages)+1)*700:900;
  group.style.minHeight=(chapterHeights.get(node.entry_id)||Math.max(900,estimate))+'px';
  outlineOnly(node,parent,heading);const location=allLocations.get(node.entry_id);locations.push(location);targets.set(node.entry_id,heading);
  chapterSlots.set(node.entry_id,{node,body:group,nav:navRows.get(node.entry_id).closest('.source-outline-group')});return group;
 }
 function forgetLocations(key){
  for(const [id,location] of allLocations)if(chapters.owner(location.nodeId)===key){allLocations.delete(id);navRows.delete(id);groups.delete(id);targets.delete(id);}
  for(let i=locations.length-1;i>=0;i--)if(chapters.owner(locations[i].nodeId)===key)locations.splice(i,1);
  offsets=null;active=null;
 }
 function replaceChapter(key,render){
  const slot=chapterSlots.get(key);if(!slot)return;
  const location=currentLocation(),oldTarget=location?.target,oldTop=oldTarget?.getBoundingClientRect().top;
  chapterObserver?.unobserve(slot.body);forgetLocations(key);
  const scratch=el('div'),next=render(slot.node,scratch);slot.body.replaceWith(next);slot.nav.replaceWith(...scratch.childNodes);
  locations.sort((a,b)=>a.target===b.target?0:a.target.compareDocumentPosition(b.target)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1);
  const anchor=allLocations.get(location?.key)?.target;
  if(body.isConnected&&!pdfMode&&!navigationChapter&&anchor&&oldTop!==undefined)content.scrollTop+=anchor.getBoundingClientRect().top-oldTop;
  offsets=null;if(!mounted.has(key))chapterObserver?.observe(next);
 }
 function trimChapters(current){
  if(!current||!lazy)return;const index=chapters.chapters.findIndex(c=>c.chapter_key===current);
  for(const key of [...mounted]){
   if(key===navigationChapter||Math.abs(chapters.chapters.findIndex(c=>c.chapter_key===key)-index)<=1||chapterLoads.has(key))continue;
   const slot=chapterSlots.get(key);chapterHeights.set(key,slot.body.getBoundingClientRect().height);mounted.delete(key);
   for(const node of nodes)if(chapters.owner(node.entry_id)===key)sectionsByNode.delete(node.entry_id);
   for(const entry of chapters.entries(key))processed.delete(entry.target_note_id);
   replaceChapter(key,renderPlaceholder);
  }
 }
 async function ensureChapter(key,explicit=false){
  if(!lazy||!key||mounted.has(key))return;
  if(chapterLoads.has(key))return chapterLoads.get(key);
  const promise=(async()=>{
   await loadEntries(chapters.entries(key));if(!alive||!isCurrent())return;
   mounted.add(key);replaceChapter(key,renderNode);status.textContent=`当前可浏览 ${structure.counts.published} 篇笔记，正文按章节加载。`;
   if(!explicit)onScroll();
  })();chapterLoads.set(key,promise);
  try{await promise;}finally{chapterLoads.delete(key);}
 }
 for(const node of children.get(null)||[])body.append(renderNode(node,context));
 function currentLocation(){if(!offsets){const top=content.getBoundingClientRect().top;offsets=locations.map(location=>({location,top:location.target.getBoundingClientRect().top-top+content.scrollTop}));}let low=0,high=offsets.length-1,chosen=0;while(low<=high){const mid=(low+high)>>1;if(offsets[mid].top<=content.scrollTop+70){chosen=mid;low=mid+1;}else high=mid-1;}return offsets[chosen]?.location;}
 function sync(immediate=false){if(!isCurrent()||pdfMode||body.hidden||!body.isConnected||navigationTarget)return;const location=currentLocation(),resume=resumePending;resumePending=false;trimChapters(chapters.owner(location?.nodeId));if(immediate||resume||location?.key===active){clearTimeout(focusTimer);pending=null;highlight(location,resume);return;}if(location?.key===pending?.key)return;pending=location;clearTimeout(focusTimer);focusTimer=setTimeout(()=>{const value=pending;pending=null;highlight(value);},70);}
 const onScroll=()=>{if(!frame)frame=requestAnimationFrame(()=>{frame=0;sync();});};content.addEventListener('scroll',onScroll,{passive:true});
 const resize=new ResizeObserver(()=>{offsets=null;onScroll();});resize.observe(body);
 status.textContent=(structure.generation_status==='PAUSED'?'生成已暂停，':'')+'当前可浏览 '+loaded.size+' 篇笔记。'+(failures?failures+' 篇暂不可用。':'');
 const controller={fingerprint,getReadingState(){const l=currentLocation();return l?{key:l.key,nodeId:l.nodeId}:null;},getOutlineState(){return follow.snapshot();},setOutlineState(value){follow.restore(value);resumePending=false;if(follow.paused)outlineMotion.cancel();},setContext(value){bindContext(value);active=null;offsets=null;},getPage(){finishNavigation();return currentLocation()?.page||1;},getLocation(){finishNavigation();return currentLocation();},
  locationForPage(page){const candidates=locations.filter(l=>l.pages?.includes(page));if(candidates.length)return candidates[0];const source=nodes.filter(v=>v.start<=page&&v.end>=page).sort((a,b)=>b.depth-a.depth)[0];return allLocations.get(source?.entry_id)||locations[0];},
  async revealPage(page){const l=controller.locationForPage(page);if(l)await controller.revealLocation(l.key);},highlightLocation(key){highlight(allLocations.get(key));},
  async revealLocation(key,offset=0){let l=allLocations.get(key);if(l){navigationChapter=chapters.owner(l.nodeId);await ensureChapter(navigationChapter,true);if(!alive||!isCurrent())return;l=allLocations.get(key)||l;jump(l.target);content.scrollTop+=offset;sync();}},
  relativeOffset(location){return location?content.scrollTop-(location.target.getBoundingClientRect().top-content.getBoundingClientRect().top+content.scrollTop-65):0;},
  setPdfNavigator(fn){pdfNavigator=fn;},setMode(mode){pdfMode=mode==='pdf';clearTimeout(focusTimer);pending=null;if(!pdfMode)sync();},highlightPage(page){const l=controller.locationForPage(page);highlight(l);return l;},
  dispose(){alive=false;navigationSequence++;chapterObserver?.disconnect();content.removeEventListener('scroll',onScroll);content.removeEventListener('wheel',interruptNavigation);content.removeEventListener('touchstart',interruptNavigation);for(const type of outlineInputs){content.removeEventListener(type,readerInput);navContext.removeEventListener(type,manualOutline);}contentMotion.cancel();outlineMotion.cancel();clearTimeout(focusTimer);resize.disconnect();cancelAnimationFrame(frame);}};
 // Offscreen figures must not delay the reader controller and PDF switching.
 for(const image of body.querySelectorAll('img')){image.loading='lazy';image.decoding='async';}
 if(lazy){
  const initial=chapters.owner(n.chapter_key)||chapters.chapters.find(c=>chapters.entries(c.chapter_key).length)?.chapter_key||chapters.chapters[0].chapter_key;
  navigationChapter=initial;await new Promise(resolve=>requestAnimationFrame(resolve));await ensureChapter(initial,true);if(!alive||!isCurrent()){controller.dispose();return null;}
  const location=allLocations.get(n.chapter_key)||allLocations.get(initial);if(location&&body.isConnected)jump(location.target);
  if(typeof IntersectionObserver!=='undefined'){chapterObserver=new IntersectionObserver(changes=>{for(const change of changes)if(change.isIntersecting&&!pdfMode)void ensureChapter(change.target.dataset.chapterKey).catch(error=>{status.textContent='章节加载失败：'+error.message;});},{root:content,rootMargin:'600px 0px'});for(const [key,slot] of chapterSlots)if(!mounted.has(key))chapterObserver.observe(slot.body);}
 }else requestAnimationFrame(()=>{if(!isCurrent()||pdfMode||body.hidden||!body.isConnected)return;if(n.chapter_key&&targets.has(n.chapter_key))jump(targets.get(n.chapter_key));else sync();});return controller;
}
