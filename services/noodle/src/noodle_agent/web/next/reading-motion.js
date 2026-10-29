export const easeOut=t=>1-Math.pow(1-Math.max(0,Math.min(1,t)),3);
export const motionDuration=distance=>Math.min(300,180+Math.abs(distance)*.12);
export function scrollMotion(host){
 let frame=0,complete=null;
 const cancel=()=>{cancelAnimationFrame(frame);frame=0;complete=null;};
 return {cancel,to(top,{immediate=false,onComplete=()=>{}}={}){
  cancel();top=Math.max(0,Math.min(top,host.scrollHeight-host.clientHeight));
  if(immediate||matchMedia('(prefers-reduced-motion: reduce)').matches||Math.abs(host.scrollTop-top)<2){host.scrollTo({top,behavior:'instant'});onComplete();return;}
  const start=host.scrollTop,time=performance.now(),duration=motionDuration(top-start);complete=onComplete;
  const step=now=>{const progress=Math.min(1,(now-time)/duration);host.scrollTo({top:start+(top-start)*easeOut(progress),behavior:'instant'});if(progress<1)frame=requestAnimationFrame(step);else{frame=0;const done=complete;complete=null;done?.();}};frame=requestAnimationFrame(step);
 }};
}
