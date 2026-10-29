import {describe,it,expect,vi} from 'vitest'
import {outlineOpen,tocScrollTop,outlineFollow,defaultOutlineOpen,sectionOwner} from '../services/noodle/src/noodle_agent/web/next/source-reading-view.js'

describe('reading-following outline',()=>{
 it('opens three visible levels and keeps a paper section with its Atomic owner',()=>{
  expect([0,1,2,3].map(defaultOutlineOpen)).toEqual([true,true,false,false]);
  const nodes=new Map([['abstract',{}],['method',{}]]),entry={parent_entry_id:'method'},anchor={node_id:'abstract'};
  expect(sectionOwner(entry,anchor,null,nodes,false)).toBe('method');
  expect(sectionOwner(entry,anchor,null,nodes,true)).toBe('abstract');
 })
 it('keeps manual browsing paused through idle time and resumes only on reader intent',()=>{
  vi.useFakeTimers();try{const follow=outlineFollow();expect(follow.paused).toBe(false);follow.pause();vi.advanceTimersByTime(20000);expect(follow.paused).toBe(true);expect(follow.resume()).toBe(true);expect(follow.paused).toBe(false);expect(follow.resume()).toBe(false);}finally{vi.useRealTimers();}
 })
 it('preserves manual browsing across a refreshed reader and accepts the latest interaction',()=>{
  const current=outlineFollow();current.pause();const refreshed=outlineFollow(current.snapshot());expect(refreshed.paused).toBe(true);
  current.resume();refreshed.restore(current.snapshot());expect(refreshed.paused).toBe(false);
  current.pause();refreshed.restore(current.snapshot());expect(refreshed.paused).toBe(true);
 })
 it('closes the previous automatic branch while keeping a manually opened sibling',()=>{
  const manual=new Map([['reference',true]]),branch=new Set(['chapter','current'])
  expect(outlineOpen('previous',manual,branch)).toBe(false)
  expect(outlineOpen('current',manual,branch)).toBe(true)
  expect(outlineOpen('reference',manual,branch)).toBe(true)
 })
 it('honours explicit collapse and keeps book parts open by default',()=>{
  const manual=new Map([['chapter',false]]),branch=new Set(['chapter'])
  expect(outlineOpen('chapter',manual,branch)).toBe(false)
  expect(outlineOpen('part',manual,branch,true)).toBe(true)
 })
 it('positions the current row centre at 38.2 percent without overscrolling either end',()=>{
  expect(tocScrollTop(400,700,40,300,500,3000)).toBe(629)
  expect(tocScrollTop(0,310,20,300,500,3000)).toBe(0)
  expect(tocScrollTop(2400,1000,40,300,500,3000)).toBe(2500)
 })
})
