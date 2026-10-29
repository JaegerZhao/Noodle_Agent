import {describe,it,expect} from 'vitest'
import {sourceEntries,sourceReadingEntries,loadSourceNotes,headingKey,readingFingerprint,atomicDisplayTitle,outlineHeadingBindings,chapterGroups} from '../services/noodle/src/noodle_agent/web/next/source-reader.js'
const group=(id,position,parent=null)=>({entry_id:id,position,parent_entry_id:parent,target_note_id:null})
const target=(id,position,parent=null,extra={})=>({...group(id,position,parent),target_note_id:id,...extra})
const note=id=>({id,kind:'ATOMIC',eligible:true,revision:{id:'r-'+id}})
describe('source reading composition',()=>{
 it('loads only the requested book chapter and checks a lightweight version before reusing its outline',async()=>{
  const paths=[],data={schema_version:'reading-manifest-v1',version:'v1',anchor_binding:'binding',document_profile:{kind:'BOOK'},chapter_tasks:[{chapter_key:'a'},{chapter_key:'b'}],outline:{nodes:[{node_id:'a',parent_id:null,title:'第一章',position:0,start:1},{node_id:'sub',parent_id:'a',title:'小节',position:1,start:1},{node_id:'b',parent_id:null,title:'第二章',position:2,start:2}]},notes:[{note_id:'n',revision_id:'r-n',creation_key:'n',title:'笔记',primary_node_id:'sub',pages:[1]}],planned_notes:[]};
  const api=async path=>{paths.push(path);return path.includes('view=version')?{version:'v1'}:data;};
  const entries=await sourceReadingEntries({creation_key:'source-map:r:live'},api,()=>true),groups=chapterGroups(entries);
  expect(groups.entries('a').map(e=>e.target_note_id)).toEqual(['n']);expect(groups.entries('b')).toEqual([]);
  expect(await sourceReadingEntries({creation_key:'source-map:r:live'},api,()=>true)).toBe(entries);
  expect(paths).toEqual(['/source-revisions/r/reading?view=manifest','/source-revisions/r/reading?view=version']);
  const calls=[];await loadSourceNotes(groups.entries('a'),async path=>{calls.push(path);return note('n');},()=>true);
  expect(calls).toEqual(['/notes/n?reading_source_revision=r']);
 })
 it('places merged Transformer subsections under their source headings without splitting the Atomic',()=>{
  const titles=['自注意力','多头注意力','位置编码','整体架构','Transformer、CNN与RNN的比较','用于图像的Transformer *','其他Transformer变体 *'];
  const nodes=[{entry_id:'root',parent_entry_id:null,title:'15.5 Transformer',position:0},...titles.map((title,i)=>({entry_id:'s'+i,parent_entry_id:'root',title:`15.5.${i+1} ${title}`,position:i+1}))];
  const headings=['Transformer 的定位与适用任务','自注意力','多头注意力','位置编码','编码器块与解码器块','Transformer、CNN 与 RNN 的对比','用于图像的 Transformer','其他 Transformer 变体'].map(title=>({title,level:3}));
  expect([...outlineHeadingBindings(headings,nodes,'root').entries()].sort((a,b)=>a[0]-b[0]).map(([i,n])=>[i,n.entry_id])).toEqual(titles.map((_,i)=>[i+1,'s'+i]));
  expect(outlineHeadingBindings([{title:'自注意力',level:3}],nodes,'other').size).toBe(0);
 })
 it('does not guess unmatched headings without uniquely bracketed source siblings',()=>{
  const nodes=[{entry_id:'root',title:'章',position:0},...['甲','乙','丙','丁'].map((title,i)=>({entry_id:title,parent_entry_id:'root',title,position:i}))];
  const bindings=outlineHeadingBindings(['甲','未命名','丁'].map(title=>({title,level:3})),nodes,'root');expect([...bindings.keys()]).toEqual([0,2]);
 })
 it('reuses only pinned revisions after a fresh reading projection and refetches changed revisions',async()=>{
  let calls=0;const api=async()=>({...note('a'),revision:{id:++calls===1?'r-a':'new'}});
  const entries=[target('a',1,null,{target_revision_id:'r-a'})];await loadSourceNotes(entries,api,()=>true);await loadSourceNotes(entries,api,()=>true);expect(calls).toBe(1);
  expect((await loadSourceNotes([target('a',1,null,{target_revision_id:'new'})],api,()=>true)).get('a').note.revision.id).toBe('new');expect(calls).toBe(2);
 })
 it('preserves the advanced star when the parent has none',()=>{expect(atomicDisplayTitle('3.3 线性高斯系统 *','3.3 线性高斯系统')).toBe('3.3 线性高斯系统 *')})
 it('uses compact labels for published and pending notes',async()=>{const entries=await sourceReadingEntries({creation_key:'source-map:r:live'},async()=>({outline:{nodes:[]},notes:[{note_id:'a',creation_key:'a',title:'原目录范围',display_title:'标量推断 *',pages:[1]}],planned_notes:[{creation_key:'b',title:'另一原目录范围',display_title:'向量推断 *',pages:[2]}]}),()=>true);expect(entries.map(e=>e.title)).toEqual(['标量推断 *','向量推断 *'])})
 it('removes only the repeated component and keeps a distinct merged subject',()=>{expect(atomicDisplayTitle('1.1 什么是机器学习？；1.2.1 分类','1.1 什么是机器学习？')).toBe('1.2.1 分类');expect(atomicDisplayTitle('1.3 无监督学习','1.3 无监督学习')).toBe('');expect(atomicDisplayTitle('软间隔 SVM','核 SVM')).toBe('软间隔 SVM')})
 it('refreshes on a new title or note revision while ignoring running task telemetry',()=>{const initial={display_title:'书名',notes:[{revision_id:'a'}],readiness:{notes_status:'PARTIAL',calls:1}};expect(readingFingerprint(initial)).toBe(readingFingerprint({...initial,readiness:{...initial.readiness,calls:2}}));expect(readingFingerprint(initial)).not.toBe(readingFingerprint({...initial,notes:[{revision_id:'b'}]}));expect(readingFingerprint(initial)).not.toBe(readingFingerprint({...initial,display_title:'中文书名'}))})
 it('identifies duplicate presentation headings while keeping distinct subjects',()=>{expect(headingKey('2.1 贝叶斯规则')).toBe(headingKey('贝叶斯规则'));expect(headingKey('核 SVM')).not.toBe(headingKey('软间隔 SVM'))})
 it('follows chapter tree order even when storage rows are unordered',()=>{const result=sourceEntries([target('b',4,'chapter2'),target('a',1,'chapter1'),group('chapter2',3),group('chapter1',0)]);expect(result.map(e=>e.entry_id)).toEqual(['chapter1','a','chapter2','b']);expect(result.map(e=>e.depth)).toEqual([0,1,0,1])})
 it('loads each active atomic identity once and skips unavailable entries',async()=>{const calls=[];const result=await loadSourceNotes([target('a',1),target('a',2),target('gone',3,null,{available:false})],async path=>{calls.push(path);return note('a')},()=>true);expect(calls).toEqual(['/notes/a']);expect(result.get('a').note.revision.id).toBe('r-a')})
 it('never silently substitutes a stale pinned revision or a Concept',async()=>{const result=await loadSourceNotes([target('a',1,null,{target_revision_id:'old'}),target('b',2)],async path=>path.endsWith('a')?note('a'):{...note('b'),kind:'CONCEPT'},()=>true);expect(result.get('a').error).toBeTruthy();expect(result.get('b').error).toBeTruthy()})
 it('keeps a failed note visible as an error while other notes load',async()=>{const result=await loadSourceNotes([target('a',1),target('b',2)],async path=>{if(path.endsWith('a'))throw Error('connection failed');return note('b')},()=>true);expect(result.get('a').error).toBe('connection failed');expect(result.get('b').note.id).toBe('b')})
 it('stops adding requests when the reading route changes',async()=>{let calls=0;await loadSourceNotes(Array.from({length:12},(_,i)=>target(String(i),i)),async path=>{calls++;return note(path.split('/').at(-1))},()=>calls<4);expect(calls).toBe(4)})
})
