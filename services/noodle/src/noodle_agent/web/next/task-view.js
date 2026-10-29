// Pure presentation rules shared by the task drawer and its regression checks.
export function duration(ms){
 if(ms==null)return '—';if(ms>0&&ms<1000)return '不足1秒';const seconds=Math.max(0,Math.floor(ms/1000)),s=seconds%60,m=Math.floor(seconds/60)%60,h=Math.floor(seconds/3600),pad=n=>String(n).padStart(2,'0');
 return h?`${h}小时${pad(m)}分钟${pad(s)}秒`:seconds>=60?`${m}分钟${pad(s)}秒`:`${s}秒`;
}
export const elapsedMs=(summary,received,now)=>summary.timing_available===false||summary.generation_timing_available===false?null:Math.max(0,(summary.active_ms??Math.max(0,(summary.wall_ms||0)-(summary.pause_ms||0)))+(summary.ticking?Math.max(0,now-received):0));
export const waitingMs=(summary,received,now)=>Math.max(0,(summary.waiting_ms||0)+(summary.waiting_ticking?Math.max(0,now-received):0));
export const visibleUsage=summary=>(summary.model_usage||[]).filter(group=>group.total_tokens>0);
export const archivedTask=job=>Boolean(job.source_deleted_at||(job.source_current_revision_id&&job.source_revision_id&&job.source_current_revision_id!==job.source_revision_id));
export function taskIssue(t){
 if(t.readiness?.generation_issue)return t.readiness.generation_issue.message;
 const error=JSON.parse(t.job.error_json||'{}');
 if(error.provider_issue)return error.provider_issue.message;
 return /Insufficient Balance|AccountOverdueError|insufficient_quota/i.test(error.detail||'')?'模型服务账户余额不足或欠费，充值后可从检查点继续。':error.detail||'当前范围需要处理，已生成内容保留。';
}
export function chapterHint(t){
 const chapters=t.readiness?.chapters||[];if(!chapters.length)return '';
 const latest=[...chapters].reverse().find(c=>c.published>0),current=t.control?.chapter_title;
 const updated=latest?'已更新至 '+latest.title+(latest.status==='READY'?'':'（部分）'):'';
 const generating=current&&t.job.status==='RUNNING'?'正在生成 '+current:'';
 if(updated&&generating&&latest.title===current)return updated+'；本章生成中';
 return [updated,generating].filter(Boolean).join('；');
}
export function backgroundItems(state){
 const c=state?.concept_maintenance;if(!c)return [];
 const structure=state.knowledge_structure?.status||'WAITING';
 const localComplete=structure!=='COMPLETE'&&state.knowledge_structure?.local_status==='COMPLETE'&&!state.knowledge_structure?.pending;
 return [
  {label:'知识归并',value:c.total?`${c.completed}/${c.total}`:'等待内容',status:c.status,note:[c.routing_pending?`${c.routing_pending}篇待归属`:'',(c.failed_routing_jobs||c.failed_build_jobs)?`${(c.failed_routing_jobs||0)+(c.failed_build_jobs||0)}个任务待处理`:''].filter(Boolean).join('；')},
  {label:'知识索引',value:c.indexed?`${c.indexed}篇就绪`:'等待知识点',status:c.completed>c.indexed?'RUNNING':c.indexed?'COMPLETE':'WAITING'},
  {label:'结构整理',value:localComplete?'本次已更新':({COMPLETE:'已整理',RUNNING:'整理中',PARTIAL:'部分待处理',WAITING:'随内容更新'}[structure]||'等待更新'),status:localComplete?'COMPLETE':structure,note:localComplete?'本次知识结构已更新':structure==='COMPLETE'&&state.knowledge_structure?.upstream_pending?'现有知识已整理，仍有内容待归并':''}
 ];
}
