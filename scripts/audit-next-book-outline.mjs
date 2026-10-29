// Read-only acceptance: source hierarchy and progress, without model requests.
import {chromium} from 'playwright-core'
import {mkdir,writeFile} from 'node:fs/promises'
import assert from 'node:assert/strict'
const base='http://127.0.0.1:8002',out='.local-only/book-planning-2026-10-22'
await mkdir(out,{recursive:true})
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true})
try{
 const page=await browser.newPage({viewport:{width:1600,height:1000}}),errors=[],writes=[]
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().includes('/internal/next/')&&r.method()!=='GET')writes.push({url:r.url(),method:r.method()})})
 const structure=await(await fetch(base+'/internal/next/source-revisions/3fa7cba2-26cd-441d-8048-728448f5b54a/outline')).json()
 await page.goto(base+'/app/#note=8e9168d1-6821-46af-bd87-77a7d4d1730d')
 await page.locator('.source-reading[aria-busy="false"]').waitFor({timeout:60000})
 assert.equal(await page.locator('.source-atomic[data-revision]').count(),50)
 assert.equal(await page.locator('.planned-note-placeholder').count(),8)
 assert.equal(await page.locator('#context .source-outline-group').count(),715)
 assert.equal(await page.getByText('后续章节 · 已发布笔记',{exact:true}).count(),0)
 const nodes=structure.outline.nodes
 assert.equal(nodes.filter(n=>n.kind==='CHAPTER').length,23)
 assert.equal(nodes.filter(n=>n.kind==='PART').length,5)
 const intro=nodes.find(n=>n.title==='1 Introduction'),second=nodes.find(n=>n.title==='2 Probability: Univariate Models')
 assert.equal(intro.status,'COMPLETE');assert.equal(second.status,'PARTIAL');assert.equal(second.planned_count,26);assert.equal(second.published_count,18)
 assert.ok(await page.locator('.source-reading .katex').count()>0)
 assert.equal(await page.locator('.source-atomic .status-error').count(),0)
 await page.screenshot({path:out+'/full-source-outline.png',fullPage:false})
 await page.locator('#context summary').filter({hasText:'III Deep Neural Networks'}).click()
 await page.locator('#context summary').filter({hasText:'13 Neural Networks for Tabular Data'}).click()
 const future=nodes.find(n=>n.title==='13 Neural Networks for Tabular Data')
 await page.waitForFunction(id=>{const h=document.getElementById('source-entry-'+id);return h&&h.getBoundingClientRect().top>0&&h.getBoundingClientRect().top<160},future.node_id)
 assert.match(await page.locator('#source-entry-'+future.node_id+' + p').innerText(),/尚未规划/)
 await page.screenshot({path:out+'/unplanned-chapter.png',fullPage:false})
 await page.setViewportSize({width:390,height:844});await page.locator('[data-panel="library"]').click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 await page.screenshot({path:out+'/mobile-outline.png',fullPage:false})
 assert.deepEqual(errors,[]);assert.deepEqual(writes,[])
 const report={original_bookmark_nodes:714,visible_outline_nodes:715,chapters:23,parts:5,published_notes:50,known_planned_notes:58,pending_notes:8,planning_status:structure.planning_status,first_chapter:intro.status,second_chapter:{status:second.status,published:18,planned:26},unplanned_chapter_navigation:true,mobile_overflow:false,automatic_generation:false,api_writes:writes,errors}
 await writeFile(out+'/browser-results.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2))
}finally{await browser.close()}
