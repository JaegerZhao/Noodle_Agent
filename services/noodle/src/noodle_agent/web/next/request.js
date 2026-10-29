// Share reads across panels; content reads still validate their current revision.
export function createApi({base='/internal/next',fetchImpl=(...args)=>fetch(...args),now=()=>performance.now(),ttl=5000}={}){
 const cached=new Map(),pending=new Map();let epoch=0;
 const reusable=path=>/^\/(?:library(?:\?|$)|sources(?:\?|$)|source-folders$|source-statuses$|workspace\/summary$|knowledge\/roots(?:\/[^/]+\/tree)?(?:\?|$)|graph\?|concept-graph\?)/.test(path);
 const invalidate=()=>{epoch++;cached.clear();pending.clear();};
 async function api(path,options={}){
  const method=(options.method||'GET').toUpperCase(),read=method==='GET',version=epoch;
  const shared=read&&!options.signal;
  if(shared){const item=cached.get(path);if(item&&now()-item.at<ttl)return item.value;if(pending.has(path))return pending.get(path);}
  else if(!read)invalidate();
  const promise=(async()=>{
   try{
    const response=await fetchImpl(base+path,{...options,headers:options.body instanceof FormData?{}:{'Content-Type':'application/json',...options.headers}});
    if(!response.ok){let error;try{error=await response.json();}catch{error={detail:response.statusText};}throw Error(typeof error.detail==='string'?error.detail:JSON.stringify(error.detail||error));}
    const value=await response.json();
    if(read&&epoch===version&&reusable(path)){cached.set(path,{value,at:now()});if(cached.size>96)cached.delete(cached.keys().next().value);}
    return value;
   }finally{if(!read)invalidate();}
  })();
  if(shared){pending.set(path,promise);try{return await promise;}finally{if(pending.get(path)===promise)pending.delete(path);}}
  return promise;
 }
 api.invalidate=invalidate;return api;
}
export const workspaceApi=createApi();
