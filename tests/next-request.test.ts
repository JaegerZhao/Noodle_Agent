import {describe,it,expect,vi} from 'vitest'
import {createApi} from '../services/noodle/src/noodle_agent/web/next/request.js'

const deferred=()=>{let resolve:any;const promise=new Promise(r=>{resolve=r});return {promise,resolve}}
const response=value=>({ok:true,json:async()=>value})
describe('navigation request sharing',()=>{
 it('shares concurrent reads, expires metadata and never reuses a current note response',async()=>{
  let time=0,calls=0;const gate=deferred(),api=createApi({now:()=>time,fetchImpl:async()=>{calls++;await gate.promise;return response(calls)}});
  const a=api('/knowledge/roots'),b=api('/knowledge/roots');gate.resolve();expect(await a).toBe(await b);expect(calls).toBe(1);
  expect(await api('/knowledge/roots')).toBe(1);time=5001;expect(await api('/knowledge/roots')).toBe(2);
  await api('/notes/current');await api('/notes/current');expect(calls).toBe(4);
 })
 it('invalidates on writes and prevents an older in-flight response from replacing new metadata',async()=>{
  const old=deferred();let calls=0;const api=createApi({fetchImpl:async(_url,options)=>{calls++;return options.method==='PUT'?response('saved'):calls===1?old.promise:response('new')}});
  const stale=api('/sources');await api('/sources/a/state',{method:'PUT'});expect(await api('/sources')).toBe('new');old.resolve(response('old'));expect(await stale).toBe('old');expect(await api('/sources')).toBe('new');expect(calls).toBe(3);
  api.invalidate();await api('/sources');expect(calls).toBe(4);
 })
 it('does not retain failed requests and keeps failures visible',async()=>{
  let fail=true;const api=createApi({fetchImpl:async()=>{if(fail){fail=false;return {ok:false,json:async()=>({detail:'Unavailable'})}}return response('ready')}});
  await expect(api('/workspace/summary')).rejects.toThrow('Unavailable');expect(await api('/workspace/summary')).toBe('ready');
 })
 it('never caches task snapshots, passes read deadlines through, and does not invalidate metadata for abortable reads',async()=>{
  const fetchImpl=vi.fn(async()=>response({})),api=createApi({fetchImpl});await api('/sources')
  const controller=new AbortController();await api('/jobs/one/timeline',{cache:'no-store',signal:controller.signal})
  await api('/jobs/one/timeline',{cache:'no-store',signal:controller.signal});await api('/generation-tasks');await api('/generation-tasks')
  await api('/sources');expect(fetchImpl).toHaveBeenCalledTimes(5)
  expect(fetchImpl.mock.calls[1][1]).toMatchObject({cache:'no-store',signal:controller.signal})
 })
 it('allows the next progress read after a hung fetch is aborted',async()=>{
  const controller=new AbortController();const api=createApi({fetchImpl:async(_url,options)=>options.signal?new Promise((_resolve,reject)=>options.signal.addEventListener('abort',()=>reject(options.signal.reason))):response('completed')})
  const pending=api('/jobs/one/timeline',{signal:controller.signal});controller.abort(Error('read timeout'))
  await expect(pending).rejects.toThrow('read timeout');expect(await api('/jobs/one/timeline')).toBe('completed')
 })
})
