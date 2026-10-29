// Upload acknowledgement is distinct from confirmation and background generation.
export function createSourceImporter({api,confirm,onState=()=>{}}){
 let active=false;
 return async function importSource(file){
  if(active)throw Error('正在处理上一份导入，请等待确认结果。');
  active=true;
  const form=new FormData();form.append('file',file);
  try{
   onState('UPLOADING',file);
   let result=await api('/sources',{method:'POST',body:form});
   if(result.duplicate&&result.deleted){
    onState('AWAITING_CONFIRMATION',file);
    if(!await confirm('重新生成资料','这份资料已删除。使用当前流程重新生成？旧笔记和调用记录保留为历史；匹配的解析结果可复用。','重新生成')){
     onState('CANCELLED',file);return {cancelled:true};
    }
    onState('REGENERATING',file);
    result=await api('/sources?regenerate_removed=true',{method:'POST',body:form});
   }
   onState(result.duplicate?'DUPLICATE':'ACCEPTED',file);
   return result;
  }finally{active=false;}
 };
}
