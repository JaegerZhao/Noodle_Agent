import {articleOutline} from './article-outline.js';
import {workspace,rememberPosition} from './workspace-state.js';
import {appendConceptSources} from './concept-source-view.js';
import {appendRelatedConcepts} from './related-concepts.js';

export async function conceptReader({note,api,el,action,md,host,context,openNote,openSource,showGraph,back,editLinks,isCurrent,options={}}){
 const main=el('article',undefined,'reader concept-reader'),top=el('header',undefined,'reader-topbar');top.append(action('返回知识体系',back),el('strong',note.title));const download=el('a','导出笔记');download.href='/internal/next/notes/'+note.id+'/export';top.append(download);main.append(top,el('span','知识点','tag'),el('h1',note.display_title||note.title));
 if(options.revision_id){const banner=el('p',undefined,'history-banner');banner.append(el('strong','正在查看历史版本 '+note.revision.ordinal+' '),action('返回当前版本',()=>openNote(note.id,{returnToKnowledge:true})));main.append(banner);}
 const tools=el('div',undefined,'reader-tools'),body=el('div',undefined,'reading concept-body'),related=el('section',undefined,'concept-related'),sources=el('section',undefined,'concept-sources');related.id='note-related';related.append(el('h2','关联知识'));sources.id='note-sources';sources.append(el('h2','来源笔记'));
 const markdown=(note.display_article_markdown||note.display_markdown||note.markdown).replace(/^# [^\n]*\n\n/,'');md(body,markdown,note);const sectionHeadings=[...body.querySelectorAll('h2')];for(const [i,s]of note.sections.entries())if(sectionHeadings[i])sectionHeadings[i].id='section-'+s.section_id;
 let disposed=false,outline=null;const alive=()=>!disposed&&isCurrent();
 tools.append(action('查看关联',()=>showGraph(note.id)),action('管理链接',()=>editLinks(note)));main.append(tools,body,related,sources);
 host.replaceChildren(main);host.scrollTop=options.revision_id?0:workspace.positions['concept:'+note.id]||0;
 outline=articleOutline({article:body,context,host,el,action,extra:[{key:related.id,title:'关联知识',target:related},{key:sources.id,title:'来源笔记',target:sources}]});
 void appendRelatedConcepts({note,api,el,action,host:related,openNote,alive});
 void appendConceptSources({note,api,el,action,host:sources,openNote,openSource,alive}).catch(error=>{if(alive())sources.append(el('p','来源读取失败：'+error.message,'muted'));});
 if(options.section_id)requestAnimationFrame(()=>outline?.jump('section-'+options.section_id));
 return {dispose(){rememberPosition('concept:'+note.id,host.scrollTop);disposed=true;outline?.dispose();}};
}
