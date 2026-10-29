import {describe,it,expect} from 'vitest'
import {taskOrder} from '../services/noodle/src/noodle_agent/web/next/task-panel.js'
import {archivedTask} from '../services/noodle/src/noodle_agent/web/next/task-view.js'

describe('generation history ordering',()=>{
 it('archives removed and superseded source tasks, and restores current-version tasks normally',()=>{
  const task={source_revision_id:'old',source_current_revision_id:'old',source_deleted_at:null};
  expect(archivedTask(task)).toBe(false);
  expect(archivedTask({...task,source_deleted_at:'removed'})).toBe(true);
  expect(archivedTask({...task,source_current_revision_id:'new'})).toBe(true);
  expect(archivedTask({...task,source_revision_id:'new',source_current_revision_id:'new'})).toBe(false);
 })
 it('keeps newest first and uses identity for equal timestamps, without mutating the input',()=>{
  const items=[{id:'a',created_at:'2026-10-25T02:00:00Z'},{id:'b',created_at:'2026-10-25T02:00:00Z'},{id:'z',created_at:'2026-10-24T02:00:00Z'}]
  expect(taskOrder(items).map(x=>x.id)).toEqual(['b','a','z'])
  expect(items.map(x=>x.id)).toEqual(['a','b','z'])
 })
})
