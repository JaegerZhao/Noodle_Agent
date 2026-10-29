import {describe,it,expect} from 'vitest';
// @ts-ignore browser-native module
import {headingTree} from '../services/noodle/src/noodle_agent/web/next/article-outline.js';
describe('actual article heading hierarchy',()=>{
 it('keeps repeated headings distinct and handles skipped heading levels',()=>{
  const tree=headingTree([{id:'core',title:'知识框架',level:2},{title:'原理',level:3},{title:'公式',level:5},{title:'原理',level:3},{title:'边界',level:2}]);
  expect(tree.map((n:any)=>n.depth)).toEqual([0,1,2,1,0]);
  expect(tree[2].parent).toBe(tree[1].key);expect(tree[3].parent).toBe('core');expect(tree[3].occurrence).toBe(2);
  expect(new Set(tree.map((n:any)=>n.key)).size).toBe(5);
 });
});
