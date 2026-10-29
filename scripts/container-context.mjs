// Check local module dependencies in the image filesystem before Docker runs.
import {readFileSync,statSync} from 'node:fs'
import {dirname,posix,resolve} from 'node:path'

export function checkHarnessContext(root){
 const copied=new Map();
 for(const line of readFileSync(resolve(root,'Dockerfile.harness'),'utf8').split(/\r?\n/)){
  if(!/^COPY\s/i.test(line))continue;
  const fields=line.trim().split(/\s+/).slice(1),destination=fields.pop();
  for(const source of fields){
   const from=resolve(root,source),folder=statSync(from).isDirectory();
   const target=posix.normalize(destination.endsWith('/')?destination+posix.basename(source):destination).replace(/^\.\//,'');
   copied.set(target, {source,folder});
  }
 }
 function available(path){
  for(const [target,item] of copied){
   if(path===target&&!item.folder)return resolve(root,item.source);
   if(item.folder&&(path===target||path.startsWith(target+'/')))return resolve(root,item.source,path.slice(target.length).replace(/^\//,''));
  }
  throw Error('Dockerfile.harness does not copy required file: '+path);
 }
 const pkg=JSON.parse(readFileSync(resolve(root,'package.json'),'utf8'));
 const build=/^node\s+(\S+)$/.exec(pkg.scripts.build);
 if(!build)throw Error('Review Harness build entry for container dependency checks');
 const pending=[build[1],'scripts/start-harness.mjs'],seen=new Set();
 while(pending.length){
  const path=pending.pop();if(seen.has(path))continue;seen.add(path);
  const content=readFileSync(available(path),'utf8');
  for(const match of content.matchAll(/(?:from\s*|import\s*\(\s*|import\s*)['"](\.[^'"]+)['"]/g)){
   pending.push(posix.normalize(posix.join(dirname(path).replaceAll('\\','/'),match[1])));
  }
 }
 for(const name of ['preset.yml','agent.cordis.yml'])readFileSync(available('config/agent-presets/noodle-knowledge/'+name));
 return {scripts:seen.size};
}
