// Read back the actual UI audit conversation; never silently create another run.
import {chromium} from 'playwright-core'
import {mkdir,writeFile} from 'node:fs/promises'
import {join} from 'node:path'
import assert from 'node:assert/strict'
const output=join(import.meta.dirname,'../.local-only/frontend-audit-2026-10-21');await mkdir(output,{recursive:true})
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true})
try{
 const page=await browser.newPage({viewport:{width:1500,height:1050}}),errors=[];page.on('pageerror',e=>errors.push(e.message))
 await page.goto('http://127.0.0.1:3080');await page.getByText(process.env.NOODLE_AGENT_AUDIT_SESSION_TITLE||'检索设计矩阵中的N和D',{exact:true}).first().click()
 await page.waitForFunction(()=>document.body.innerText.includes('noodle_answer_knowledge')&&document.body.innerText.includes('样本'))
 const text=await page.locator('body').innerText(),tools=[...new Set(text.match(/noodle_[a-z_]+/g)||[])]
 for(const tool of ['noodle_search_knowledge','noodle_answer_knowledge','noodle_get_atomic_note'])assert.ok(tools.includes(tool))
 assert.match(text,/来源/);assert.match(text,/仅查询|只做了检索/);assert.deepEqual(errors,[])
 const report={status:'ACTUAL_SESSION_READBACK_PASSED',text,tools,errors,url:page.url()};await writeFile(join(output,'agent.json'),JSON.stringify(report,null,2));await page.screenshot({path:join(output,'agent-rag.png'),fullPage:true});console.log(JSON.stringify({status:report.status,tools,errors}))
}finally{await browser.close()}
