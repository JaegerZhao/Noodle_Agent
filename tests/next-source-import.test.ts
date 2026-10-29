import {it,expect,vi} from 'vitest'
import {createSourceImporter} from '../services/noodle/src/noodle_agent/web/next/source-import.js'
const file=()=>new File(['%PDF-fixture'],'ML-Slides6.pdf',{type:'application/pdf'})

it('distinguishes waiting for confirmation from uploading and only regenerates once approved',async()=>{
 let approve:any;const states:string[]=[],gate=new Promise<boolean>(resolve=>approve=resolve)
 const api=vi.fn().mockResolvedValueOnce({duplicate:true,deleted:true}).mockResolvedValueOnce({source_id:'s',revision_id:'new',regenerated:true})
 const run=createSourceImporter({api,confirm:()=>gate,onState:state=>states.push(state)})
 const pending=run(file());await Promise.resolve()
 expect(states).toEqual(['UPLOADING','AWAITING_CONFIRMATION']);expect(api).toHaveBeenCalledTimes(1)
 await expect(run(file())).rejects.toThrow('上一份导入');approve(true)
 expect(await pending).toMatchObject({revision_id:'new'});expect(states).toEqual(['UPLOADING','AWAITING_CONFIRMATION','REGENERATING','ACCEPTED'])
 expect(api.mock.calls[1][0]).toBe('/sources?regenerate_removed=true')
})
it('cancelling regeneration never restores the old source or starts a task',async()=>{
 const api=vi.fn().mockResolvedValue({duplicate:true,deleted:true}),states:string[]=[]
 const run=createSourceImporter({api,confirm:async()=>false,onState:state=>states.push(state)})
 expect(await run(file())).toEqual({cancelled:true});expect(api).toHaveBeenCalledTimes(1);expect(states.at(-1)).toBe('CANCELLED')
})
it('a failed upload can be retried without automatic duplicate writes',async()=>{
 const api=vi.fn().mockRejectedValueOnce(Error('Unavailable')).mockResolvedValueOnce({source_id:'s'})
 const run=createSourceImporter({api,confirm:async()=>true})
 await expect(run(file())).rejects.toThrow('Unavailable');expect(api).toHaveBeenCalledTimes(1)
 expect(await run(file())).toEqual({source_id:'s'})
})
