// One reading scope shared by the central page and the library sidebar.
const key='noodle.workspace.v1';
const read=()=>{try{return JSON.parse(sessionStorage.getItem(key)||'{}');}catch{return {};}};
const initial=read();
export const workspace={kind:initial.kind||'SOURCES',folder:initial.folder||'all',sort:initial.sort||'created_desc',rootId:initial.rootId||'all',query:'',positions:initial.positions||{}};
export function updateScope(value){Object.assign(workspace,value);try{sessionStorage.setItem(key,JSON.stringify(workspace));}catch{}return workspace;}
export function scopedRoute(query){const p=new URLSearchParams(query);if(workspace.kind==='SOURCES'){p.delete('root');p.set('folder',workspace.folder);p.set('sort',workspace.sort);}else{p.delete('folder');p.delete('sort');p.set('root',workspace.rootId);}return p.toString();}
export function readScope(params){if(params.has('folder'))updateScope({kind:'SOURCES',folder:params.get('folder')||'all',sort:params.get('sort')||'created_desc'});else if(params.has('root'))updateScope({kind:'KNOWLEDGE',rootId:params.get('root')||'all'});}
export function rememberPosition(key,top){updateScope({positions:{...workspace.positions,[key]:top}});}
export function creationLabel(stamp,now=new Date()){
 const date=new Date(stamp);if(!stamp||!Number.isFinite(date.getTime()))return '时间未知';const minutes=Math.floor((now-date)/60000);
 if(minutes>=0&&minutes<1)return '刚刚';if(minutes>=0&&minutes<60)return `${minutes}分钟前`;
 if(date.toDateString()===now.toDateString())return date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});
 return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
}
