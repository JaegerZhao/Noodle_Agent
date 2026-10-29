import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
const root=process.argv[2];
if(!root)throw new Error('Provide the acceptance output directory');
const state=fs.readFileSync('services/noodle/src/noodle_agent/web/next/markdown.js','utf8');
const expression=name=>vm.runInNewContext(state.split('\n').find(s=>s.startsWith(`export const ${name} = `)).replace(`export const ${name} = `,'').replace(/;\s*$/,''));
const context={module:{exports:{}},exports:{}};
vm.runInNewContext(fs.readFileSync('services/noodle/src/noodle_agent/web/vendor/katex.min.js','utf8'),context);
let checked=0;const errors=[];
for(const note of JSON.parse(fs.readFileSync(path.join(root,'notes-for-render-check.json'),'utf8'))){
 const body=note.markdown.replace(expression('codeTokenPattern'),'');
 for(const m of body.matchAll(expression('mathTokenPattern'))){checked++;try{context.module.exports.renderToString(m[1]??m[2]??m[3]??m[4],{throwOnError:true,strict:'ignore',trust:false});}catch(e){errors.push({title:note.title,expression:m[0],error:e.message});}}
}
fs.writeFileSync(path.join(root,'math-render-check.json'),JSON.stringify({checked,errors},null,2));
console.log(JSON.stringify({checked,errors:errors.length}));process.exitCode=Number(errors.length>0);
