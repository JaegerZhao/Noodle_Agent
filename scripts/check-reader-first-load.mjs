// Offline DOM regression with published notes; no browser or provider calls.
// Usage: node scripts/check-reader-first-load.mjs fixture.json linkedom/esm/index.js
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
import path from 'node:path';

const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const {parseHTML,NodeFilter}=await import(pathToFileURL(path.resolve(process.argv[3])).href);
const {window,document,Node,HTMLElement}=parseHTML('<!doctype html><html><body><main></main><aside></aside></body></html>');
Object.defineProperty(document,'compatMode',{value:'CSS1Compat'});
const storage=new Map();
Object.assign(globalThis,{window,document,Node,NodeFilter,localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},matchMedia:()=>({matches:true}),requestAnimationFrame:fn=>setTimeout(()=>fn(performance.now()),0),cancelAnimationFrame:clearTimeout,ResizeObserver:class{observe(){}disconnect(){}},IntersectionObserver:class{observe(){}unobserve(){}disconnect(){}}});
for(const [name,file] of [['marked','marked.umd.js'],['katex','katex.min.js']]){
 const module={exports:{}};vm.runInNewContext(fs.readFileSync('services/noodle/src/noodle_agent/web/vendor/'+file,'utf8'),{module,exports:module.exports,document,console});window[name]=module.exports;
}
// Inputs are trusted published fixture bytes. Production uses real DOMPurify.
window.DOMPurify={sanitize:html=>html};
const content=document.querySelector('main'),context=document.querySelector('aside');
const nodes=fixture.manifest.outline.nodes,positions=new Map(nodes.map((n,i)=>[n.node_id,(i+1)*4000]));
Object.defineProperties(HTMLElement.prototype,{clientHeight:{get(){return 600}},clientWidth:{get(){return 800}},scrollHeight:{get(){return 5000000}}});
HTMLElement.prototype.scrollTo=function({top}){this.scrollTop=top;this.dispatchEvent(new window.Event('scroll'));};
HTMLElement.prototype.getBoundingClientRect=function(){
 let top=0,height=30;
 if(this!==content&&this!==context){
  if(context.contains(this)){top=([...context.querySelectorAll('.source-outline-link')].indexOf(this)+1)*30-(context.scrollTop||0);}
  else {const heading=this.id?.startsWith('source-entry-')?this:this.closest('.source-topic')?.querySelector('[id^="source-entry-"]');top=(positions.get(heading?.id?.replace('source-entry-',''))||0)-(content.scrollTop||0);height=this.matches('.source-topic')?20000:30;}
 }
 return {top,bottom:top+height,height,width:800,left:0,right:800};
};
HTMLElement.prototype.getClientRects=function(){return [this.getBoundingClientRect()];};
content.scrollTop=0;context.scrollTop=0;
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const action=(title,fn)=>{const n=el('button',title);n.onclick=fn;return n;};
const {renderMarkdown}=await import('../services/noodle/src/noodle_agent/web/next/markdown.js');
const {sourceReadingEntries}=await import('../services/noodle/src/noodle_agent/web/next/source-reader.js');
const {composeSourceReader}=await import('../services/noodle/src/noodle_agent/web/next/source-reading-view.js');
const requests=[];
const api=async url=>{requests.push(url);if(url.includes('view=version'))return {version:fixture.manifest.version};if(url.includes('view=manifest'))return fixture.manifest;const id=url.match(/^\/notes\/([^?]+)/)?.[1];assert.ok(fixture.notes[id],url);return fixture.notes[id];};
const md=(target,text,note)=>renderMarkdown(target,text,{imageSizes:note.display_image_sizes,imageSource:ref=>'/internal/next/'+ref.replace(/^\.\.\//,'')});
const article=el('article');content.append(article);const started=performance.now();
const initialChapter=process.argv[4]?fixture.manifest.chapter_tasks.find(c=>c.title.startsWith(process.argv[4])):fixture.manifest.chapter_tasks[0];assert.ok(initialChapter,'requested initial chapter exists');
const reader=await composeSourceReader(article,{creation_key:'source-map:'+fixture.revision+':live',chapter_key:initialChapter.chapter_key},{api,el,action,md,context,content,isCurrent:()=>true});
const firstNotes=requests.filter(p=>p.startsWith('/notes/')).length;
assert.equal(firstNotes,fixture.manifest.notes.filter(n=>n.chapter_key===initialChapter.chapter_key).length);
assert.equal(context.querySelectorAll('.source-outline-link').length,nodes.length+article.querySelectorAll('.source-generated-heading').length);
const firstDomNodes=article.querySelectorAll('*').length;
const firstRenderedMath=article.querySelectorAll('.katex').length;
if(process.argv[4])assert.ok(firstRenderedMath>0,'real KaTeX rendering is exercised');
const firstMs=performance.now()-started;
async function visit(title){const node=nodes.find(n=>n.title.startsWith(title));assert.ok(node,title);await context.querySelector(`[data-location-key="${node.node_id}"]`).onclick();return {node,section:document.getElementById('source-entry-'+node.node_id)?.parentElement};}
for(const title of ['14.2.1 ','14.2.2 ','14.2.3 ','14.2.4 ','15.5.1 ','15.5.2 ']){
 const {section}=await visit(title);assert.ok(section.querySelector('.source-atomic-body')?.textContent.trim().length>60,title+' must contain generated text');
}
const toggle=context.querySelector('.source-outline-toggle');toggle.onclick();const manuallyOpen=toggle.getAttribute('aria-expanded');
await visit('第21章');
assert.equal(toggle.getAttribute('aria-expanded'),manuallyOpen,'manual expansion is preserved');
assert.equal(fixture.manifest.chapter_tasks.filter(c=>!document.getElementById('source-entry-'+c.chapter_key).parentElement.classList.contains('source-chapter-placeholder')).length,1,'only neighbouring visited chapters remain mounted');
context.dispatchEvent(new window.Event('wheel'));assert.equal(reader.getOutlineState().manual,true);
await new Promise(r=>setTimeout(r,90));assert.equal(reader.getOutlineState().manual,true,'idle does not resume outline following');
const target=nodes.find(n=>n.title.startsWith('15.5.1 '));
reader.setMode('pdf');const pdfLocation=reader.locationForPage(target.start);reader.setMode('notes');await reader.revealLocation(pdfLocation.key,73);
assert.ok(Math.abs(reader.relativeOffset(reader.locationForPage(target.start))-73)<1,'notes/PDF section offset is restored');
const count=requests.length;await sourceReadingEntries({creation_key:'source-map:'+fixture.revision+':live'},api,()=>true);assert.deepEqual(requests.slice(count),['/source-revisions/'+fixture.revision+'/reading?view=version']);
const images=[...article.querySelectorAll('img')];assert.ok(images.every(img=>img.getAttribute('src').startsWith('/internal/next/')));
reader.dispose();
console.log(JSON.stringify({initial_chapter:initialChapter.title,first_notes:firstNotes,total_notes:Object.keys(fixture.notes).length,first_dom_nodes:firstDomNodes,first_rendered_math:firstRenderedMath,full_outline:nodes.length,offline_dom_first_ms:Math.round(firstMs),checks:['chapter hydration','14.2 and 15.5 text','chapter eviction','manual TOC retention','PDF offset restoration','version-only refresh','asset URLs'],provider_calls:0},null,2));
