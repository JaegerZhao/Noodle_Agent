import {afterEach,it,expect,vi} from 'vitest'
import {createNotice} from '../services/noodle/src/noodle_agent/web/next/notice.js'
function setup(){
 vi.useFakeTimers()
 const host={children:[] as any[],classList:{toggle:vi.fn()},replaceChildren(){this.children=[]},append(...values:any[]){this.children.push(...values)}}
 const el=(tag:string,text:string,cls:string)=>({tag,text,cls,setAttribute:vi.fn(),onclick:null})
 return {host,show:createNotice({host,el})}
}
afterEach(()=>vi.useRealTimers())
it('dismisses after five seconds without an older timer removing a newer notice',()=>{
 const {host,show}=setup();show('旧提示');vi.advanceTimersByTime(4000);show('新提示');vi.advanceTimersByTime(1000)
 expect(host.children[0].text).toBe('新提示');vi.advanceTimersByTime(3999);expect(host.children).toHaveLength(2)
 vi.advanceTimersByTime(1);expect(host.children).toEqual([])
})
it('provides an accessible close button and clears its timer',()=>{
 const {host,show}=setup();show('提示',true);expect(host.children[1].setAttribute).toHaveBeenCalledWith('aria-label','关闭提示')
 host.children[1].onclick();expect(host.children).toEqual([]);expect(vi.getTimerCount()).toBe(0)
})
it('empty messages remove the notice immediately',()=>{const {host,show}=setup();show('处理中');show('');expect(host.children).toEqual([]);expect(vi.getTimerCount()).toBe(0)})
