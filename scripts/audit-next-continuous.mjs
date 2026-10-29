// Read-only real-library acceptance: full source body and wheel-driven PDF pages.
import {chromium} from 'playwright-core'
import {mkdir,writeFile} from 'node:fs/promises'
import assert from 'node:assert/strict'
const base='http://127.0.0.1:8002',out='.local-only/continuous-reading-2026-10-21'
await mkdir(out,{recursive:true})
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true})
try{
 const page=await browser.newPage({viewport:{width:1600,height:1000}}),errors=[],downloads=[];page.on('pageerror',e=>errors.push(e.message));page.on('download',d=>downloads.push(d.suggestedFilename()))
 const map=await(await fetch(base+'/internal/next/notes/8e9168d1-6821-46af-bd87-77a7d4d1730d')).json()
 const all=await(await fetch(base+'/internal/next/library?kind=ATOMIC&source_revision='+map.creation_key.split(':')[1]+'&limit=500')).json();const expected=all.items.map(n=>n.id)
 await page.goto(base+'/app/#note='+map.id)
 await page.locator('.source-reading[aria-busy="false"]').waitFor({timeout:60000})
 assert.equal(await page.locator('.source-atomic[data-revision]').count(),expected.length)
 assert.ok((await page.locator('.source-reading').innerText()).length>10000)
 assert.ok(await page.locator('.source-reading .katex').count()>0)
 assert.equal(await page.locator('.source-reading .status-error').count(),0)
 assert.equal(await page.locator('article.reader h1').count(),1)
 await page.screenshot({path:out+'/source-continuous.png',fullPage:false})
 const noteId='2bdbc178-145b-4720-87b1-859006fe9b4f'
 await page.locator('#context .source-outline-link').filter({hasText:'最大似然估计与负对数似然损失'}).click()
 assert.ok(await page.locator('.next-pdf-pane').count()===0,'note directory stays in note mode')
 await page.waitForFunction(id=>{const n=document.getElementById('source-note-'+id);return n.getBoundingClientRect().top>0&&n.getBoundingClientRect().top<180},noteId)
 await page.screenshot({path:out+'/source-formula.png',fullPage:false})
 // Per-note editing keeps the atomic revision; opening and cancelling never writes.
 await page.locator('#source-note-'+noteId).getByRole('button',{name:'编辑这一节',exact:true}).first().click()
 assert.equal(await page.locator('#editTitle').inputValue(),'最大似然估计与负对数似然损失');await page.getByRole('button',{name:'取消',exact:true}).click()
 await page.getByRole('button',{name:'原始 PDF',exact:true}).click()
 await page.locator('canvas[data-page="38"]').waitFor({state:'visible',timeout:60000})
 assert.equal(await page.locator('.pdf-page-shell').count(),860)
 const view=page.locator('.pdf-canvas-view');await view.hover();await page.mouse.wheel(0,1600)
 await page.waitForFunction(()=>Number(document.querySelector('[aria-label="PDF 页码"]').value)>38)
 const afterWheel=Number(await page.getByLabel('PDF 页码',{exact:true}).inputValue());assert.ok(afterWheel>=39)
 await page.locator('canvas[data-page="'+afterWheel+'"]').waitFor({state:'visible'})
 await page.screenshot({path:out+'/pdf-continuous-scroll.png',fullPage:false})
 await page.getByLabel('PDF 页码',{exact:true}).fill('400');await page.getByLabel('PDF 页码',{exact:true}).press('Enter');await page.locator('canvas[data-page="400"]').waitFor({state:'visible'})
 await page.waitForTimeout(600)
 assert.ok(await page.locator('.pdf-page-shell canvas').count()<=9,'distant canvases are evicted')
 // While viewing PDF, the source directory navigates within the same PDF.
 await page.locator('#context .source-outline-link').filter({hasText:'最大似然估计与负对数似然损失'}).click();await page.locator('canvas[data-page="38"]').waitFor({state:'visible'})
 await page.locator('.content-mode-switch').getByRole('button',{name:'笔记',exact:true}).click();await page.locator('.source-reading').waitFor({state:'visible'})
 await page.waitForFunction(id=>document.getElementById('source-note-'+id).getBoundingClientRect().top<180,noteId)
 await page.setViewportSize({width:390,height:844});await page.locator('[data-panel="reader"]').click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 await page.screenshot({path:out+'/mobile-notes.png',fullPage:false})
 await page.getByRole('button',{name:'原始 PDF',exact:true}).click();await page.locator('canvas[data-page="38"]').waitFor({state:'visible'})
 await page.waitForFunction(()=>{const c=document.querySelector('canvas[data-page="38"]');return c&&c.width<500&&Math.abs(c.getBoundingClientRect().width/c.getBoundingClientRect().height-c.width/c.height)<.01})
 await view.hover();await page.mouse.wheel(0,650);await page.waitForFunction(()=>Number(document.querySelector('[aria-label="PDF 页码"]').value)>38)
 await page.screenshot({path:out+'/mobile-pdf.png',fullPage:false});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 await page.setViewportSize({width:1600,height:1000});await page.locator('#files').click();await page.locator('.source-table').waitFor();await page.locator('.source-actions').getByRole('button',{name:'原始 PDF',exact:true}).click();await page.locator('.next-pdf-pane').waitFor({state:'visible'});await page.locator('canvas[data-page="1"]').waitFor({state:'visible'})
 assert.deepEqual(errors,[]);assert.deepEqual(downloads,[])
 const report={source_notes:expected.length,source_revision:map.revision.id,source_body_continuous:true,atomic_editor_identity:true,pdf_pages:860,wheel_page:afterWheel,pdf_jump:400,pdf_canvas_limit:9,pdf_directory_sync:true,mobile_scroll:true,source_pdf_action:true,errors,downloads};await writeFile(out+'/results.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2))
}catch(error){console.error(error);throw error}finally{await browser.close()}
