// Follow-up context holds current Note identities, never previous answer facts.
export function scopeKey(scope){return JSON.stringify({mode:scope.mode,source_ids:[...(scope.source_ids||[])].sort(),note_ids:[...(scope.note_ids||[])].sort()});}
export function isFollowUp(text){return /^(?:它(?:们)?|这个公式|该公式|这个方法|上述|上面|前面|继续|再解释|再用|再举|举个例子|用一个(?:简单)?例子)/.test(text.trim());}
export class QuestionContext{
 saved=null;
 clear(){this.saved=null;}
 begin(question,scope){
  const key=scopeKey(scope);
  if(scope.mode==='NOTES')return {key,entity_note_ids:[...scope.note_ids],followup:false,titles:[]};
  if(isFollowUp(question)&&this.saved?.key===key)return {key,entity_note_ids:this.saved.ids,followup:true,titles:this.saved.titles};
  return {key,entity_note_ids:[],followup:false,titles:[]};
 }
 accept(request,response){
  if(response.status!=='READY'||!response.citations?.length){this.clear();return;}
  const unique=[...new Map(response.citations.map(c=>[c.note_id,c])).values()].slice(0,3);
  this.saved={key:request.key,ids:unique.map(c=>c.note_id),titles:unique.map(c=>c.title)};
 }
}
export function codePointSlice(text,start,end){return Array.from(text).slice(start,end).join('');}
export function citedCounts(counts){return [counts.atomic_notes?`${counts.atomic_notes} 篇原子笔记`:null,counts.concept_notes?`${counts.concept_notes} 个知识点`:null].filter(Boolean).join('、');}
