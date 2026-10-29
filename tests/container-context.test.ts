import {afterEach,expect,it} from 'vitest'
import {mkdtempSync,mkdirSync,writeFileSync,rmSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {checkHarnessContext} from '../scripts/container-context.mjs'

const temporary:string[]=[]
afterEach(()=>{for(const dir of temporary.splice(0))rmSync(dir,{recursive:true,force:true})})
it('checks image build dependencies rather than files available only in the checkout',()=>{
 const root=mkdtempSync(join(tmpdir(),'noodle-image-context-'));temporary.push(root)
 for(const dir of ['scripts','config/agent-presets/noodle-knowledge'])mkdirSync(join(root,dir),{recursive:true})
 const write=(name:string,text:string)=>writeFileSync(join(root,name),text)
 write('package.json',JSON.stringify({scripts:{build:'node scripts/build-plugin.mjs'}}))
 write('scripts/build-plugin.mjs',"await import('./build-client.mjs')")
 write('scripts/build-client.mjs','export {}')
 write('scripts/start-harness.mjs','export {}')
 for(const name of ['preset.yml','agent.cordis.yml'])write('config/agent-presets/noodle-knowledge/'+name,'preset')
 const base='COPY package.json ./\nCOPY scripts/start-harness.mjs ./scripts/start-harness.mjs\n'
 write('Dockerfile.harness',base)
 expect(()=>checkHarnessContext(root)).toThrow('scripts/build-plugin.mjs')
 write('Dockerfile.harness',base+'COPY scripts/build-plugin.mjs ./scripts/build-plugin.mjs\n')
 expect(()=>checkHarnessContext(root)).toThrow('scripts/build-client.mjs')
 write('Dockerfile.harness',base+'COPY scripts ./scripts\n')
 expect(()=>checkHarnessContext(root)).toThrow('config/agent-presets')
 write('Dockerfile.harness',base+'COPY scripts ./scripts\nCOPY config ./config\n')
 expect(checkHarnessContext(root).scripts).toBe(3)
})
