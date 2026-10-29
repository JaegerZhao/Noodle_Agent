import {describe,it,expect} from 'vitest';
import {directFamily} from '../services/noodle/src/noodle_agent/web/next/related-concepts.js';
describe('direct Concept family',()=>{
 it('keeps multiple direct parents and children, excluding siblings, non-tree links and missing/ineligible nodes',()=>{
  const nodes=['center','p1','p2','child','sibling'].map(id=>({id,title:id,kind:'CONCEPT'}));nodes.push({id:'atomic',title:'原文',kind:'ATOMIC'});
  const edge=(source,target,navigation_role='TOPIC_CHILD')=>({source,target,navigation_role,status:'CONFIRMED'});
  const family=directFamily('center',nodes,[edge('p1','center'),edge('p2','center'),edge('p1','center'),edge('center','child'),edge('p1','sibling'),edge('center','sibling',null),edge('center','atomic'),edge('center','missing'),{...edge('center','sibling'),status:'SUGGESTED'}]);
  expect(family.parents.map(n=>n.id)).toEqual(['p1','p2']);expect(family.children.map(n=>n.id)).toEqual(['child']);
 });
 it('does not invent hierarchy for an isolated Concept',()=>expect(directFamily('alone',[{id:'alone',title:'独立',kind:'CONCEPT'}],[])).toEqual({parents:[],children:[]}));
});
