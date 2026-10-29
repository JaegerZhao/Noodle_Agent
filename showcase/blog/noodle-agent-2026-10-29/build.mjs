// Build a portable article package from project diagrams and original screenshots.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
const here=path.dirname(fileURLToPath(import.meta.url));
const repo=path.resolve(here,'../../..');
const require=createRequire(import.meta.url);
const bundled='C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const resolve=name=>require.resolve(name,{paths:[here,bundled]});
const sharp=require(resolve('sharp'));
const assets=path.join(here,'assets');
await fs.mkdir(assets,{recursive:true});
const diagrams=['01-knowledge-evolution','02-atomic-generation','03-concept-maintenance','04-agent-architecture','05-rag-retrieval','06-invalidation','07-index-build'];
const shotRoot='showcase/blog/noodle-agent-2026-10-29/source-screenshots/';
const shots=[
 {source:shotRoot+'atomic-reading.png',target:'08-atomic-reading.png',providedDate:'2026-10-29'},
 {source:shotRoot+'concept-sources.png',target:'09-concept-sources.png',providedDate:'2026-10-29'},
 {source:shotRoot+'task-stages.png',target:'11-task-stages.png',providedDate:'2026-10-29'},
 {source:shotRoot+'workspace-overview.png',target:'12-workspace-overview.png',providedDate:'2026-10-29'},
 {source:shotRoot+'knowledge-systems.png',target:'13-knowledge-systems.png',providedDate:'2026-10-29'},
 {source:shotRoot+'transformer-content.png',target:'14-transformer-content.png',providedDate:'2026-10-29'},
 {source:shotRoot+'knowledge-graph.png',target:'15-knowledge-graph.png',providedDate:'2026-10-29'},
];
const manifest=[];
const digest=b=>createHash('sha256').update(b).digest('hex');
for(const name of diagrams){
 const source=path.join(repo,'docs/project-introduction-assets-2026-10-29',name+'.svg');
 const bytes=await fs.readFile(source);
 await fs.writeFile(path.join(assets,name+'.svg'),bytes);
 const view=bytes.toString().match(/viewBox="0 0 ([\d.]+) ([\d.]+)"/);
 const width=Math.round(Number(view[1])*2);
 await sharp(bytes,{density:144}).resize({width}).png().toFile(path.join(assets,name+'.png'));
 manifest.push({asset:'assets/'+name+'.png',vector:'assets/'+name+'.svg',source:path.relative(repo,source).replaceAll('\\','/'),sourceDate:'2026-10-29',sourceSha256:digest(bytes),type:'diagram-render',width,height:Math.round(Number(view[2])*2)});
}
for(const item of shots){
 const {target}=item;
 const source=path.join(repo,item.source);
 const bytes=await fs.readFile(source);await fs.writeFile(path.join(assets,target),bytes);
 if(digest(await fs.readFile(path.join(assets,target)))!==digest(bytes))throw Error('Screenshot copy changed: '+target);
 const meta=await sharp(bytes).metadata();
 manifest.push({asset:'assets/'+target,source:item.source,...(item.providedDate?{providedDate:item.providedDate}:{sourceDate:item.sourceDate}),sha256:digest(bytes),type:'original-screenshot',width:meta.width,height:meta.height});
}
const md=await fs.readFile(path.join(here,'article.md'),'utf8');
await fs.writeFile(path.join(here,'asset-manifest.json'),JSON.stringify(manifest,null,2),'utf8');
const images=[...md.matchAll(/!\[[^\]]*\]\(([^)]+)\)/g)].map(x=>x[1]);
for(const image of images)await fs.access(path.join(here,image));
const main=md.split('## 附录 A：')[0];
const faq=md.split('## 附录 B：')[1]||'';
const counts={images:images.length,chapters:[...main.matchAll(/^## [1-8]\. /gm)].length,appendices:[...md.matchAll(/^## 附录 /gm)].length,chineseCharacters:[...md.matchAll(/[\u4e00-\u9fff]/g)].length,bodyChineseCharacters:[...main.matchAll(/[\u4e00-\u9fff]/g)].length,interviewQuestions:[...faq.matchAll(/^### \d+\. /gm)].length,originalScreenshots:shots.length,userProvidedScreenshots:shots.filter(s=>s.providedDate).length};
if(counts.chapters!==8||counts.images!==14)throw Error('Unexpected article structure');
await fs.writeFile(path.join(here,'verification.json'),JSON.stringify({checked:'2026-10-29',...counts,missingImages:0,screenshotHashesMatch:true,externalAssets:0},null,2));
console.log(JSON.stringify(counts));
