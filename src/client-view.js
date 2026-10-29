import React,{useEffect,useRef} from 'react';
import {renderMarkdown} from '../services/noodle/src/noodle_agent/web/next/markdown.js';
import {parseToolJson,citationSources} from '../services/noodle/src/noodle_agent/web/next/agent-client.js';
const loads=new Map();
function assets(base){
 if(loads.has(base))return loads.get(base);
 const load=(name,global)=>window[global]?Promise.resolve():new Promise((resolve,reject)=>{const s=document.createElement('script');s.src=base+'static/vendor/'+name;s.onload=resolve;s.onerror=reject;document.head.append(s);});
 const css=document.createElement('link');css.rel='stylesheet';css.href=base+'static/vendor/katex.min.css';document.head.append(css);
 const value=Promise.all([load('marked.umd.js','marked'),load('purify.min.js','DOMPurify'),load('katex.min.js','katex')]);loads.set(base,value);return value;
}
function AnswerView({block}){
  const ref=useRef(null);let value=null;
 const settled=Array.isArray(block.content);
 const raw=settled?block.content.filter(c=>c.type==='text').map(c=>c.text).join('\n'):'';
 if(settled){try{value=parseToolJson(raw);}catch{}}
 const base=value?.workbench_url;
 useEffect(()=>{let active=true;const node=ref.current;if(!node||!value)return;const body=value.answer_markdown||value.answer||value.message||value.coverageWarning||'未取得可用答案';node.textContent=body;
  if(base&&/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(?::\d+)?\/app\/$/.test(base))assets(base).then(()=>{if(active)renderMarkdown(node,body);}).catch(()=>{});
  return()=>{active=false;};
 },[block]);
 if(!value)return React.createElement('div',{className:'noodle-answer','data-noodle-kind':block.kind||'running'},settled?(raw||'调用已结束，未返回可显示结果。'):'正在检索知识并核对引用…');
 return React.createElement('section',{className:'noodle-answer','data-noodle-status':value.status},
  React.createElement('style',{},'.noodle-answer{border:1px solid #dce4ef;border-radius:8px;padding:16px;margin:10px 0;font-size:14px;line-height:1.8;overflow-wrap:anywhere}.noodle-answer h2{font-size:18px}.noodle-answer h3{font-size:16px}.noodle-answer .katex-display{overflow-x:auto;max-width:100%}.noodle-answer a{display:block;padding:5px 0;color:#5878ba}.noodle-answer table{border-collapse:collapse}.noodle-answer td,.noodle-answer th{border:1px solid #aab4c744;padding:5px 8px}'),
  React.createElement('div',{ref}),
  value.status!=='READY'?React.createElement('small',{},value.status):null,
  ...(value.citations||[]).map(c=>React.createElement('a',{key:c.citation_id,href:base+'#answer='+encodeURIComponent(value.request_id)+'&citation='+encodeURIComponent(c.citation_id),target:'_blank',rel:'noopener noreferrer'},'['+c.citation_id+'] '+c.title,React.createElement('small',{style:{display:'block',color:'#7c899e'}},citationSources(c,value.source_titles||{}).join('；'))))
 );
}
export const inject=['slots','sessions'];
export function apply(ctx){
 ctx.slots.inject('tool.call.toolview',()=>ctx.slots.register({name:'tool.call.toolview',key:'noodle_answer_knowledge'},AnswerView));
 const target=new URL(location.href).searchParams.get('noodleSession');
 if(target){const sessions=ctx.get('sessions');let opened=false;const select=()=>{if(opened||!sessions.list.getSnapshot().byId[target])return;opened=true;sessions.open(target);const url=new URL(location.href);url.searchParams.delete('noodleSession');history.replaceState(history.state,'',url);};ctx.effect(()=>sessions.list.subscribe(select));select();}
}
