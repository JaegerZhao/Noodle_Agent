import {describe,it,expect} from 'vitest';
import {renderScopeSidebar} from '../services/noodle/src/noodle_agent/web/next/scope-sidebar.js';
import {workspace} from '../services/noodle/src/noodle_agent/web/next/workspace-state.js';

describe('sidebar request recovery',()=>{
 it('ends the loading state after failure and retries the current scope successfully',async()=>{
  workspace.kind='SOURCES';let calls=0;const attributes={},host={dataset:{},children:[],setAttribute(k,v){attributes[k]=v;},replaceChildren(...nodes){this.children=nodes;},append(node){this.children.push(node);}},count={textContent:''};
  const el=(tag,text,css)=>({tag,text,css}),action=(label,run)=>({label,run});
  await renderScopeSidebar({api:async()=>{if(++calls===1)throw Error('offline');return {items:[]};},el,action,host,count,query:'',selected:null,openSource:()=>{},openNote:()=>{},isCurrent:()=>true});
  expect(attributes['aria-busy']).toBe('false');expect(host.children[0].text).toContain('目录读取失败');expect(host.children[1].label).toBe('重试目录');
  await host.children[1].run();expect(calls).toBe(2);expect(host.children[0].text).toBe('当前范围还没有可读笔记。');expect(attributes['aria-busy']).toBe('false');
 });
});
