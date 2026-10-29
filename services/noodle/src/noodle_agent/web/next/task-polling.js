// Task snapshots refresh independently of navigation and workspace statistics.
export const TASK_SNAPSHOT_FRESH_MS=15000;
export function createProgressPolling({refresh,visible,expanded,setTimer=setTimeout,clearTimer=clearTimeout,onError=()=>{}}){
 let stopped=true,timer=null,running=null,again=false;
 const schedule=()=>{clearTimer(timer);if(!stopped&&visible())timer=setTimer(()=>{void request();},expanded()?3000:15000);};
 function request(){
  if(stopped||!visible())return Promise.resolve();
  if(running){again=true;return running;}
  clearTimer(timer);
  running=Promise.resolve().then(refresh).catch(onError).finally(()=>{
   running=null;const repeat=again;again=false;
   if(repeat&&!stopped&&visible())void request();else schedule();
  });
  return running;
 }
 return {start(){stopped=false;return request();},request,wake(){clearTimer(timer);if(visible())return request();},stop(){stopped=true;again=false;clearTimer(timer);}};
}
export function snapshotNow(received,now){return Math.min(now,received+TASK_SNAPSHOT_FRESH_MS);}
export const snapshotStale=(received,now)=>now-received>TASK_SNAPSHOT_FRESH_MS;
