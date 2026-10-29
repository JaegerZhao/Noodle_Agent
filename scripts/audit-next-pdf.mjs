import {chromium} from 'playwright-core'
import {writeFile} from 'node:fs/promises'
import assert from 'node:assert/strict'
const out='.local-only/frontend-audit-2026-10-21'
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true})
try{
 const page=await browser.newPage({viewport:{width:1600,height:1000}}),errors=[],downloads=[]
 page.on('pageerror',e=>errors.push(e.message));page.on('download',d=>downloads.push(d.suggestedFilename()))
 await page.goto('http://127.0.0.1:8002/app/#note=8e9168d1-6821-46af-bd87-77a7d4d1730d')
 await page.locator('article.reader .reader-topbar').waitFor();await page.getByRole('button',{name:'原始 PDF',exact:true}).click()
 await page.locator('.next-pdf-pane canvas[data-page]').first().waitFor({state:'visible',timeout:60000})
 await page.getByLabel('PDF 页码',{exact:true}).fill('1');await page.getByLabel('PDF 页码',{exact:true}).press('Enter');await page.locator('canvas[data-page="1"]').waitFor({state:'visible'})
 await page.getByRole('button',{name:'PDF 下一页',exact:true}).click();await page.locator('canvas[data-page="2"]').waitFor({state:'visible'})
 await page.getByLabel('PDF 页码',{exact:true}).fill('33');await page.getByLabel('PDF 页码',{exact:true}).press('Enter');await page.locator('canvas[data-page="33"]').waitFor({state:'visible'})
 await page.screenshot({path:out+'/actual-pdf-js.png',fullPage:true})
 const painted=await page.locator('canvas[data-page="33"]').evaluate(c=>{const data=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let dark=0;for(let i=0;i<data.length;i+=404)if(data[i]<180&&data[i+1]<180&&data[i+2]<180)dark++;return dark;});assert.ok(painted>20)
 await page.setViewportSize({width:390,height:844});await page.waitForFunction(()=>{const c=document.querySelector('canvas[data-page="33"]');return c&&c.getBoundingClientRect().width<=innerWidth})
 await page.waitForFunction(()=>{const c=document.querySelector('canvas[data-page="33"]');return c&&c.width<500&&Math.abs(c.getBoundingClientRect().width/c.getBoundingClientRect().height-c.width/c.height)<0.01})
 await page.screenshot({path:out+'/actual-pdf-js-mobile.png',fullPage:true})
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 assert.equal(downloads.length,0);assert.deepEqual(errors,[])
 const report={rendered_pages:[1,2,33],status:await page.locator('.pdf-reader-status').innerText(),dark_pixel_samples:painted,mobile_aspect_ratio_preserved:true,downloads,errors}
 await writeFile(out+'/pdf-verified.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report))
}finally{await browser.close()}
