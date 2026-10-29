import {describe,it,expect} from 'vitest';
// @ts-ignore browser-native ES module
import {creationLabel,workspace,updateScope,scopedRoute,readScope} from '../services/noodle/src/noodle_agent/web/next/workspace-state.js';
describe('workspace reading scope',()=>{
 it('retains a folder in source links and does not leak it into knowledge links',()=>{
  updateScope({kind:'SOURCES',folder:'course',sort:'title_asc'});
  expect(scopedRoute('source=revision')).toContain('folder=course');
  updateScope({kind:'KNOWLEDGE',rootId:'machine-learning'});
  expect(scopedRoute('note=concept&folder=course')).not.toContain('folder=');
  readScope(new URLSearchParams('source=revision&folder=unfiled&sort=created_asc'));
  expect(workspace.kind).toBe('SOURCES');expect(workspace.folder).toBe('unfiled');
 });
 it('formats elapsed minutes before calendar boundaries and preserves unknown timestamps',()=>{
  const now=new Date('2026-10-28T00:15:00+08:00');
  expect(creationLabel('2026-10-27T23:55:00+08:00',now)).toBe('20分钟前');
  expect(creationLabel('2026-10-25T12:00:00+08:00',now)).toBe('2026-10-25');
  expect(creationLabel('invalid',now)).toBe('时间未知');
 });
});
