export function createNotice({host,el,delay=5000}){
 let timer=null;
 function clear(){if(timer!==null)clearTimeout(timer);timer=null;host.replaceChildren();host.classList.toggle('status-error',false);}
 return function show(message,error=false){
  clear();if(!message)return;
  const text=el('span',message,'notice-message'),close=el('button','×','notice-close');
  close.type='button';close.title='关闭提示';close.setAttribute('aria-label','关闭提示');close.onclick=clear;
  host.classList.toggle('status-error',error);host.append(text,close);
  timer=setTimeout(clear,delay);
 };
}
