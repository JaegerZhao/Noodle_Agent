import {describe,it,expect} from 'vitest'
import {duration,elapsedMs,waitingMs,visibleUsage,chapterHint,backgroundItems,taskIssue} from '../services/noodle/src/noodle_agent/web/next/task-view.js'
import {easeOut,motionDuration} from '../services/noodle/src/noodle_agent/web/next/reading-motion.js'

describe('task clock and reported usage',()=>{
 it('keeps the initial queue clock separate when the first execution attempt starts',()=>{
  const queued={active_ms:0,waiting_ms:60000,waiting_ticking:true,ticking:false}
  expect(elapsedMs(queued,0,2000)).toBe(0);expect(waitingMs(queued,0,2000)).toBe(62000)
  const running={active_ms:5000,waiting_ms:62000,ticking:true}
  expect(elapsedMs(running,0,2000)).toBe(7000);expect(waitingMs(running,0,2000)).toBe(62000)
 })
 it('formats seconds without rounding into nonexistent extra minutes',()=>{
  expect(duration(59999)).toBe('59秒');expect(duration(60000)).toBe('1分钟00秒')
  expect(duration(3599999)).toBe('59分钟59秒');expect(duration(3605000)).toBe('1小时00分钟05秒')
  expect(duration(402)).toBe('不足1秒')
 })
 it('ticks locally between server snapshots and freezes paused or finished clocks',()=>{
  expect(elapsedMs({active_ms:61000,ticking:true},100,2100)).toBe(63000)
  expect(elapsedMs({active_ms:61000,ticking:false},100,2100)).toBe(61000)
  expect(elapsedMs({wall_ms:100000,pause_ms:70000,ticking:false},0,2000)).toBe(30000)
  expect(elapsedMs({active_ms:null,generation_timing_available:false,ticking:false},0,2000)).toBeNull()
  expect(elapsedMs({active_ms:0,timing_available:false,ticking:false},0,2000)).toBeNull()
 })
 it('shows only categories with reported consumption and keeps unknown data intact',()=>{
  const summary={model_usage:[{kind:'LLM_CALL',total_tokens:12},{kind:'VISION_CALL',total_tokens:0,missing_usage:2},{kind:'EMBEDDING_CALL',total_tokens:5}]}
  expect(visibleUsage(summary).map(x=>x.kind)).toEqual(['LLM_CALL','EMBEDDING_CALL'])
  expect(summary.model_usage[1].missing_usage).toBe(2)
 })
})
describe('chapter and background summaries',()=>{
 it('explains Ark billing stops and keeps incomplete routing visible',()=>{
  expect(taskIssue({job:{error_json:JSON.stringify({detail:'403 AccountOverdueError'})}})).toContain('充值')
  const items=backgroundItems({concept_maintenance:{completed:19,total:22,indexed:19,routing_pending:78,status:'PARTIAL',failed_routing_jobs:24,failed_build_jobs:3},knowledge_structure:{status:'COMPLETE',upstream_pending:81}})
  expect(items[0].note).toContain('27个任务待处理');expect(items[2].note).toContain('仍有内容待归并')
 })
 it('uses the last published chapter rather than calling all planned chapters complete',()=>{
  const t={job:{status:'RUNNING'},control:{chapter_title:'第6章 信息论'},readiness:{chapters:[{title:'第4章',published:8,status:'READY'},{title:'第5章',published:2,status:'PARTIAL'},{title:'第6章',published:0,status:'GENERATING'}]}}
  expect(chapterHint(t)).toBe('已更新至 第5章（部分）；正在生成 第6章 信息论')
  expect(chapterHint({...t,job:{status:'PAUSED'}})).not.toContain('正在生成')
 })
 it('distinguishes concise knowledge maintenance counters',()=>{
  const items=backgroundItems({concept_maintenance:{completed:18,total:21,indexed:18,routing_pending:3,status:'PARTIAL'},knowledge_structure:{status:'WAITING'}})
  expect(items.map(x=>x.value)).toEqual(['18/21','18篇就绪','随内容更新'])
 })
 it('labels local completion without claiming current global coverage',()=>{
  const state={concept_maintenance:{completed:2,total:2,indexed:2,status:'COMPLETE'},knowledge_structure:{status:'PARTIAL',local_status:'COMPLETE',pending:0}}
  const item=backgroundItems(state)[2]
  expect(item.value).toBe('本次已更新');expect(item.note).toBe('本次知识结构已更新')
  expect(backgroundItems({...state,knowledge_structure:{...state.knowledge_structure,pending:1}})[2].value).toBe('部分待处理')
 })
})
describe('bounded reading motion',()=>{
 it('eases to the target and keeps large document jumps brief',()=>{
  expect(easeOut(0)).toBe(0);expect(easeOut(1)).toBe(1);expect(easeOut(.5)).toBeGreaterThan(.5)
  expect(motionDuration(100000)).toBe(300);expect(motionDuration(0)).toBe(180)
 })
})
