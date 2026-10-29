import {spawnSync} from 'node:child_process'
import {existsSync,readdirSync,unlinkSync} from 'node:fs'
import {dirname,join,resolve} from 'node:path'
import {fileURLToPath} from 'node:url'

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..')
await import('./build-graph.mjs')
await import('./build-agent-client.mjs')
const result=spawnSync(process.execPath,[join(root,'node_modules/typescript/bin/tsc'),'-p',join(root,'tsconfig.json')],{cwd:root,stdio:'inherit',windowsHide:true})
if(result.status!==0)process.exit(result.status??1)
// Remove only stale generated modules whose TypeScript source was retired.
const output=resolve(root,'dist')
if(existsSync(output))for(const entry of readdirSync(output,{withFileTypes:true})){
 if(!entry.isFile()||!/(?:\.d\.ts|\.js)(?:\.map)?$/.test(entry.name))continue
 const stem=entry.name.replace(/(?:\.d\.ts|\.js)(?:\.map)?$/,'')
 const target=resolve(output,entry.name)
 if(dirname(target)!==output)throw Error('Build output escaped dist')
 if(stem!=='client'&&!existsSync(join(root,'src',stem+'.ts')))unlinkSync(target)
}
