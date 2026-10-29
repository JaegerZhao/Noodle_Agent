import {afterEach,expect,it,vi} from 'vitest'
import {createProgressPolling as createTaskPolling,snapshotNow,snapshotStale} from '../services/noodle/src/noodle_agent/web/next/task-polling.js'
import {elapsedMs} from '../services/noodle/src/noodle_agent/web/next/task-view.js'

afterEach(()=>vi.useRealTimers())
const deferred=()=>{let resolve:any;const promise=new Promise(r=>resolve=r);return {promise,resolve}}

it('observes planning, writing and completion without navigating away from an open drawer',async()=>{
 vi.useFakeTimers();let server='planning',shown='';const refresh=vi.fn(async()=>{shown=server})
 const poll=createTaskPolling({refresh,visible:()=>true,expanded:()=>true})
 await poll.start();expect(shown).toBe('planning');server='writing'
 await vi.advanceTimersByTimeAsync(3000);expect(shown).toBe('writing');server='complete'
 await vi.advanceTimersByTimeAsync(3000);expect(shown).toBe('complete');expect(refresh).toHaveBeenCalledTimes(3);poll.stop()
})
it('sleeps while hidden and refreshes immediately when the tab becomes visible again',async()=>{
 vi.useFakeTimers();let visible=true;const refresh=vi.fn(async()=>{})
 const poll=createTaskPolling({refresh,visible:()=>visible,expanded:()=>true});await poll.start()
 visible=false;poll.wake();await vi.advanceTimersByTimeAsync(60000);expect(refresh).toHaveBeenCalledTimes(1)
 visible=true;await poll.wake();expect(refresh).toHaveBeenCalledTimes(2);poll.stop()
})
it('refreshes the source list on entry, including while task statistics are unavailable',async()=>{
 vi.useFakeTimers();let view='reader',published=0,shown=0;const statistics=new Promise(()=>{})
 const refresh=vi.fn(async()=>{shown=published}),poll=createTaskPolling({refresh,visible:()=>view==='files',expanded:()=>true})
 void statistics;await poll.start();expect(refresh).not.toHaveBeenCalled();view='files';await poll.wake()
 published=1;await vi.advanceTimersByTimeAsync(3000);expect(shown).toBe(1)
 view='reader';await vi.advanceTimersByTimeAsync(30000);expect(refresh).toHaveBeenCalledTimes(2);poll.stop()
})
it('uses a lighter collapsed cadence and stops completely when disposed',async()=>{
 vi.useFakeTimers();let expanded=false;const refresh=vi.fn(async()=>{})
 const poll=createTaskPolling({refresh,visible:()=>true,expanded:()=>expanded});await poll.start()
 await vi.advanceTimersByTimeAsync(14999);expect(refresh).toHaveBeenCalledTimes(1)
 await vi.advanceTimersByTimeAsync(1);expect(refresh).toHaveBeenCalledTimes(2)
 expanded=true;await poll.wake();await vi.advanceTimersByTimeAsync(3000);expect(refresh).toHaveBeenCalledTimes(4)
 poll.stop();await vi.advanceTimersByTimeAsync(60000);expect(refresh).toHaveBeenCalledTimes(4)
})
it('coalesces concurrent wakeups and recovers after a failed read without overlapping snapshots',async()=>{
 vi.useFakeTimers();const gate=deferred(),errors=vi.fn();let active=0,maxActive=0
 const refresh=vi.fn(async()=>{active++;maxActive=Math.max(maxActive,active);try{if(refresh.mock.calls.length===1)await gate.promise;if(refresh.mock.calls.length===2)throw Error('timeout')}finally{active--}})
 const poll=createTaskPolling({refresh,visible:()=>true,expanded:()=>true,onError:errors});const first=poll.start()
 poll.wake();poll.wake();await vi.advanceTimersByTimeAsync(30000);expect(refresh).toHaveBeenCalledTimes(1)
 gate.resolve();await first;await vi.advanceTimersByTimeAsync(0);expect(refresh).toHaveBeenCalledTimes(2);expect(errors).toHaveBeenCalledTimes(1)
 await vi.advanceTimersByTimeAsync(3000);expect(refresh).toHaveBeenCalledTimes(3);expect(maxActive).toBe(1);poll.stop()
})
it('does not extrapolate a stale running snapshot indefinitely, and accepts a terminal server clock',()=>{
 const running={active_ms:5000,ticking:true}
 expect(elapsedMs(running,0,snapshotNow(0,60000))).toBe(20000);expect(snapshotStale(0,60000)).toBe(true)
 const finished={active_ms:13000,ticking:false};expect(elapsedMs(finished,60000,snapshotNow(60000,65000))).toBe(13000)
 expect(snapshotStale(60000,65000)).toBe(false)
})
