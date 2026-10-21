import { createServer } from 'node:http'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import { NoodleHttpProvider } from '../src/http-provider.js'

let server: ReturnType<typeof createServer>
let ctx: Context
let requests: { path: string; body: Record<string, unknown> }[]
let stale = false

beforeEach(async () => {
  requests = []; stale = false
  server = createServer(async (request, response) => {
    let text = ''; for await (const part of request) text += part
    const path = request.url ?? ''; const body = text ? JSON.parse(text) : {}
    requests.push({ path, body })
    response.setHeader('Content-Type', 'application/json')
    if (path.includes('/source-revisions/')) {
      response.statusCode = stale ? 409 : 200
      response.end(JSON.stringify(stale ? { detail: 'not current' } : { source_id: 's1' })); return
    }
    if (path.endsWith('/search')) {
      response.end(JSON.stringify({status:'READY',counts:{atomic_notes:0,concept_notes:1},hits:[{kind:'CONCEPT',note_id:'c1',revision_id:'r1',title:'梯度',body:'知识正文',provenance:[{page:9}]}]}));return
    }
    if (path.includes('/notes/')) {
      response.end(JSON.stringify({id:'n1',kind:'ATOMIC',title:'测试',revision:{id:'r1'},markdown:'😀解释',sections:[{section_id:'s',start_offset:1,end_offset:3,provenance:[{page:2}]}]})); return
    }
    if (path.includes('/concepts/')) {
      response.end(JSON.stringify({id:'c1',kind:'CONCEPT',title:'梯度',revision:{id:'r1'},markdown:'😀解释',sections:[{section_id:'s',start_offset:1,end_offset:3}]}));return
    }
    response.statusCode = 404; response.end('{}')
  })
  await new Promise<void>(resolve => server.listen(0,'127.0.0.1',resolve))
  const address = server.address(); if (!address || typeof address === 'string') throw Error('no server')
  ctx = new Context()
  await ctx.plugin(NoodleHttpProvider,{baseUrl:`http://127.0.0.1:${address.port}`,apiPrefix:'/internal/next',retrievalMode:'KEYWORD_ONLY',timeoutMs:2000,maxBodyChars:1000})
})
afterEach(async () => {await ctx.fiber.dispose(); await new Promise<void>((resolve,reject)=>server.close(error=>error?reject(error):resolve()))})

it('preserves Concept counts and uses published-note locators rather than raw quotes',async()=>{
  const value=await ctx.noodle.searchKnowledge('梯度',5)
  expect(value).toMatchObject({status:'READY',atomicCount:0,conceptCount:1,count:1})
  expect(value.hits[0]).toMatchObject({entityType:'CONCEPT',conceptId:'c1',evidence:[{pageLabel:'9',quote:'',level:'PUBLISHED_NOTE'}]})
})
it('resolves a selected source before search and carries explicit entity IDs',async()=>{
  await ctx.noodle.searchKnowledge('它是什么',5,{sourceVersionId:'sr1',entityNoteIds:['n1']})
  expect(requests[1]?.body).toMatchObject({scope:{mode:'SOURCE',source_ids:['s1']},allowed_kinds:['ATOMIC'],entity_note_ids:['n1'],retrieval_mode:'KEYWORD_ONLY'})
})
it('fails closed when selected source is stale',async()=>{
  stale=true
  await expect(ctx.noodle.searchKnowledge('梯度',5,{sourceVersionId:'sr1'})).rejects.toMatchObject({status:409})
  expect(requests).toHaveLength(1)
})
it('uses Unicode codepoint ranges for note and Concept sections',async()=>{
  expect((await ctx.noodle.getNote('n1')).blocks[0]?.text).toBe('解释')
  expect((await ctx.noodle.getConcept('c1')).blocks[0]?.content).toBe('解释')
})
it('rejects historical pins and source-scoped Concept reads',async()=>{
  await expect(ctx.noodle.getConcept('c1',undefined,'old')).rejects.toMatchObject({status:409})
  const count=requests.length
  await expect(ctx.noodle.getConcept('c1',{sourceVersionId:'sr1'})).rejects.toMatchObject({status:409})
  expect(requests).toHaveLength(count)
})
