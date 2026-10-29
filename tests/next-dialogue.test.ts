import {describe,it,expect} from 'vitest'
import {QuestionContext,codePointSlice,citedCounts} from '../services/noodle/src/noodle_agent/web/next/dialogue.js'
const global={mode:'GLOBAL'},source={mode:'SOURCE',source_ids:['pdf-a']}
const ready={status:'READY',citations:[{note_id:'a',title:'最大似然'},{note_id:'b',title:'负对数似然'},{note_id:'a',title:'最大似然'}]}
describe('RAG dialogue identity context',()=>{
 it('binds an explicit current-note scope without requiring a prior answer',()=>{const c=new QuestionContext();expect(c.begin('它如何计算？',{mode:'NOTES',note_ids:['a']}).entity_note_ids).toEqual(['a'])})
 it('uses cited identities for follow-ups, not for new standalone questions',()=>{const c=new QuestionContext();c.accept(c.begin('最大似然是什么？',global),ready);expect(c.begin('它如何计算？',global).entity_note_ids).toEqual(['a','b']);expect(c.begin('高斯分布是什么？',global).entity_note_ids).toEqual([])})
 it('never carries a global topic into another source or note scope',()=>{const c=new QuestionContext();c.accept(c.begin('最大似然是什么？',global),ready);expect(c.begin('它如何计算？',source).entity_note_ids).toEqual([]);expect(c.begin('它如何计算？',{mode:'NOTES',note_ids:['other']}).entity_note_ids).toEqual(['other'])})
 it('clears old topics after a refusal or explicit reset',()=>{const c=new QuestionContext();c.accept(c.begin('问题',global),ready);c.accept(c.begin('未知问题',global),{status:'COVERAGE_GAP',citations:[]});expect(c.begin('它如何计算？',global).entity_note_ids).toEqual([]);c.accept(c.begin('问题',source),ready);c.clear();expect(c.begin('它如何计算？',source).entity_note_ids).toEqual([])})
 it('keeps the context bounded and source-local',()=>{const c=new QuestionContext();c.accept(c.begin('问题',source),{status:'READY',citations:Array.from({length:8},(_,i)=>({note_id:String(i),title:String(i)}))});expect(c.begin('再用一个例子解释',source).entity_note_ids).toHaveLength(3);expect(c.begin('继续',{mode:'SOURCE',source_ids:['pdf-b']}).entity_note_ids).toEqual([])})
 it('uses Python-compatible code point ranges for non-BMP characters',()=>{expect(codePointSlice('甲😀乙𝑥丙',2,4)).toBe('乙𝑥')})
 it('does not report zero atomic notes for a concept-only answer',()=>{expect(citedCounts({atomic_notes:0,concept_notes:1})).toBe('1 个知识点')})
})
