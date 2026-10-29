import {describe,it,expect} from 'vitest'
import {notesLabel,backgroundLabel,sourceReadable} from '../services/noodle/src/noodle_agent/web/next/pipeline-status.js'
describe('product readiness is independent of queue and background work',()=>{
 it('allows finished indexed Atomics while Concept is still building',()=>{const state={notes_status:'READY',concept_maintenance:{status:'RUNNING',completed:1,total:3,indexed:1,routing_pending:2}};expect(notesLabel(state)).toContain('笔记生成完成');expect(backgroundLabel(state)).toContain('后台归并中')})
 it('never calls unindexed or partial results complete',()=>{expect(notesLabel({notes_status:'INDEX_PENDING'})).not.toContain('生成完成');expect(notesLabel({notes_status:'PARTIAL'})).toContain('部分');expect(notesLabel(null)).toContain('待核实')})
 it('opens newly generated notes using current readiness even when the original list row had no Atomics',()=>{
  const row={atomic_count:0};expect(sourceReadable(row,undefined)).toBe(false)
  expect(sourceReadable(row,{readable:true,notes_status:'PARTIAL'})).toBe(true)
  expect(sourceReadable({atomic_count:8},{readable:false})).toBe(false)
 })
})
