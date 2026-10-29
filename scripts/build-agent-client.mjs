import {build} from 'esbuild';
import {writeFile,mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
const root=resolve(import.meta.dirname,'..');
const result=await build({entryPoints:[resolve(root,'src/client-view.js')],bundle:true,format:'cjs',platform:'browser',target:'es2022',external:['react'],write:false});
await mkdir(resolve(root,'dist'),{recursive:true});
await writeFile(resolve(root,'dist/client.js'),'window.__ModuleLoader__.load({id:"@noodle/agent",factory:(require)=>{var module={exports:{}};var exports=module.exports;\n'+result.outputFiles[0].text+'\nreturn module.exports;}});\n');
