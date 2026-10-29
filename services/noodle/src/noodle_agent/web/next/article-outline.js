import {scrollMotion} from './reading-motion.js';
import {renderTitle} from './markdown.js';

export function headingTree(items){const stack=[],seen=new Map();return items.map((item,index)=>{while(stack.length&&stack.at(-1).level>=item.level)stack.pop();const count=(seen.get(item.title)||0)+1;seen.set(item.title,count);const entry={...item,key:item.id||'heading-'+index,parent:stack.at(-1)?.key||null,depth:stack.length,occurrence:count};stack.push(entry);return entry;});}

export function articleOutline({article,context,host,el,action,extra=[]}){
 const occurrences=new Map();const headings=[...article.querySelectorAll('h2,h3,h4,h5,h6')],entries=headingTree(headings.map(h=>{const title=h.dataset.outlineTitle||h.textContent.trim(),count=(occurrences.get(title)||0)+1;occurrences.set(title,count);let hash=2166136261;for(const c of title)hash=Math.imul(hash^c.codePointAt(0),16777619);return {id:h.id||'article-heading-'+(hash>>>0).toString(16)+'-'+count,title,level:Number(h.tagName[1]),target:h};}));for(const e of entries)e.target.id=e.key;
 const all=[...entries,...extra.map(e=>({...e,parent:null,depth:0}))],byId=new Map(all.map(e=>[e.key,e])),children=new Map(),manual=new Map(),buttons=new Map();let active=null,pinnedKey=null,follow=true,frame=0,positions=[];
 for(const e of all){if(!children.has(e.parent))children.set(e.parent,[]);children.get(e.parent).push(e);}
 const contentMotion=scrollMotion(host),navMotion=scrollMotion(context);
 function parents(key){const result=new Set();while(key&&byId.has(key)){result.add(key);key=byId.get(key).parent;}return result;}
 function render(){const previous=context.scrollTop,branch=parents(active);buttons.clear();context.replaceChildren(el('h2','文章目录'));
  function draw(parent){for(const e of children.get(parent)||[]){const row=el('div',undefined,'source-outline-row article-outline-row');row.style.paddingLeft=(e.depth*10)+'px';const nested=children.get(e.key)||[],opened=manual.has(e.key)?manual.get(e.key):e.depth<2||branch.has(e.key);
   if(nested.length){const arrow=action(opened?'⌄':'›',()=>{manual.set(e.key,!opened);render();});arrow.className='source-outline-toggle';arrow.setAttribute('aria-expanded',String(opened));arrow.setAttribute('aria-label',(opened?'收起 ':'展开 ')+e.title);row.append(arrow);}else row.classList.add('source-outline-leaf');
   const link=action('',()=>jump(e.key));link.className='source-outline-link article-outline-link';renderTitle(link,e.title);link.classList.toggle('active',active===e.key);link.classList.toggle('active-ancestor',active!==e.key&&branch.has(e.key));if(active===e.key)link.setAttribute('aria-current','location');row.append(link);context.append(row);buttons.set(e.key,link);if(opened)draw(e.key);}}
  draw(null);context.scrollTop=previous;
 }
 function measure(){const origin=host.getBoundingClientRect().top;positions=all.map(e=>({key:e.key,top:e.target.getBoundingClientRect().top-origin+host.scrollTop})).sort((a,b)=>a.top-b.top);update();}
 function update(){frame=0;const threshold=host.scrollTop+44;let next=positions[0]?.key;for(const p of positions){if(p.top>threshold)break;next=p.key;}if(host.scrollTop+host.clientHeight>=host.scrollHeight-2)next=positions.at(-1)?.key||next;next=pinnedKey||next;if(next===active)return;active=next;render();const button=buttons.get(active);if(follow&&button){navMotion.to(context.scrollTop+button.getBoundingClientRect().top-context.getBoundingClientRect().top-context.clientHeight*.382);}}
 function jump(key){const e=byId.get(key);if(!e)return;follow=true;pinnedKey=key;update();e.onOpen?.();contentMotion.to(host.scrollTop+e.target.getBoundingClientRect().top-host.getBoundingClientRect().top-20);}
 const onScroll=()=>{if(!frame)frame=requestAnimationFrame(update);},pause=()=>{follow=false;navMotion.cancel();},resume=()=>{follow=true;pinnedKey=null;};
 const stopMotion=()=>contentMotion.cancel();for(const event of ['wheel','touchstart','pointerdown']){host.addEventListener(event,resume,{passive:true});context.addEventListener(event,pause,{passive:true});}host.addEventListener('wheel',stopMotion,{passive:true});host.addEventListener('scroll',onScroll,{passive:true});
 const observer=new ResizeObserver(measure);observer.observe(article);if(article.parentElement)observer.observe(article.parentElement);render();requestAnimationFrame(measure);
 return {jump,dispose(){observer.disconnect();cancelAnimationFrame(frame);contentMotion.cancel();navMotion.cancel();host.removeEventListener('scroll',onScroll);host.removeEventListener('wheel',stopMotion);for(const event of ['wheel','touchstart','pointerdown']){host.removeEventListener(event,resume);context.removeEventListener(event,pause);}}};
}
