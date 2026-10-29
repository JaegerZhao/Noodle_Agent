export const sourceReadable=(source,state)=>state?Boolean(state.readable):Boolean(source.atomic_count);
export function notesLabel(state){
 if(!state)return '状态待核实';
 const labels={READY:'笔记生成完成，可浏览与检索',INDEX_PENDING:'笔记可读，正在建立语义索引',PARTIAL:'部分笔记可读',NO_CONTENT:'没有需要生成的知识正文',GENERATING:'正在生成笔记',PAUSED:'已暂停',REVIEW:'需要检查',WAITING:'等待处理'};
 return labels[state.notes_status]||'状态待核实';
}
export function backgroundLabel(state){const c=state?.concept_maintenance;if(!c)return '后台状态待核实';const progress=`已发布 ${c.completed}/${c.total}，已索引 ${c.indexed}，待归属 ${c.routing_pending}`;return c.status==='WAITING'?'等待 Atomic 发布后开始知识归并':c.status==='COMPLETE'?'知识点归并与索引完成':c.status==='PAUSED'?'任务已暂停；知识点 '+progress:c.status==='PARTIAL'?'知识点部分完成，有待处理项目；'+progress:`知识点后台归并中：${progress}`;}
