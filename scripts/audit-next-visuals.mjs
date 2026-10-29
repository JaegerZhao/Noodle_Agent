// UI rendering inventory and responsive checks; private screenshots stay local.
import {chromium} from 'playwright-core'
import {mkdir,writeFile} from 'node:fs/promises'
import {join} from 'node:path'
const base=process.env.NOODLE_SMOKE_URL||'http://127.0.0.1:8002'
const out=join(import.meta.dirname,'../.local-only/frontend-audit-2026-10-21')
await mkdir(out,{recursive:true})
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true})
const errors=[],notes=[],screens=[],responsive=[]
try{
 const lib=await (await fetch(base+'/internal/next/library?limit=500')).json()
 let cursor=0
 async function worker(){const page=await browser.newPage({viewport:{width:1600,height:1000}});page.on('pageerror',e=>errors.push(e.message));
  while(cursor<lib.items.length){const n=lib.items[cursor++];await page.goto(base+'/app/#note='+n.id);await page.waitForFunction(title=>document.querySelector('article.reader>h1')?.textContent===title,n.title);await page.waitForFunction(()=>[...document.querySelectorAll('article.reader .reading img')].every(i=>i.complete));
   const d=await page.locator('article.reader').evaluate(root=>{const images=[...root.querySelectorAll('.reading img')].map(i=>({src:i.getAttribute('src'),alt:i.getAttribute('alt'),width:i.naturalWidth,height:i.naturalHeight}));const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);const raw=[];while(walker.nextNode()){const n=walker.currentNode;if(!n.parentElement.closest('.katex,code,pre,math')&&/\\(?:frac|sum|mathbb|begin)\b|\$\$/.test(n.textContent))raw.push(n.textContent.slice(0,150));}return {headings:root.querySelectorAll(':scope>h1').length,math:root.querySelectorAll('.katex').length,math_warnings:[...root.querySelectorAll('.math-render-warning')].map(n=>n.textContent),images,tables:root.querySelectorAll('.reading table').length,raw_math:raw,overflow:document.documentElement.scrollWidth>innerWidth};});
   notes.push({id:n.id,title:n.title,kind:n.kind,...d});
   for(const [label,condition] of [['image',d.images.length>0],['table',d.tables>0],['formula',d.math>8]])if(condition&&!screens.includes(label)){screens.push(label);await page.screenshot({path:join(out,'reading-'+label+'.png'),fullPage:true});}
  }
  await page.close()
 }
 await Promise.all([worker(),worker()])
 const page=await browser.newPage();page.on('pageerror',e=>errors.push(e.message))
 for(const width of [1600,1280,1024,860,768,390,320]){
  await page.setViewportSize({width,height:1000});await page.goto(base+'/app/');await page.locator('.source-table').waitFor();
  const check=async view=>responsive.push({width,view,...await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth,unlabelled:[...document.querySelectorAll('input:not([type=hidden]),select,textarea')].filter(n=>n.getClientRects().length&&!n.labels?.length&&!n.getAttribute('aria-label')).map(n=>n.id),blank_buttons:[...document.querySelectorAll('button')].filter(n=>n.getClientRects().length&&!n.textContent.trim()&&!n.getAttribute('aria-label')).map(n=>n.id)}))});
  await check('sources');await page.screenshot({path:join(out,`sources-${width}.png`),fullPage:true});
  if(width<=860)await page.locator('[data-panel="agent"]').click();await check('assistant');
  await page.locator('#question').fill('视觉审查中的问题草稿');await page.screenshot({path:join(out,`assistant-${width}.png`),fullPage:true});
 }
 const report={notes,responsive,errors,totals:{notes:notes.length,images:notes.reduce((n,x)=>n+x.images.length,0),math:notes.reduce((n,x)=>n+x.math,0),tables:notes.reduce((n,x)=>n+x.tables,0)},issues:notes.filter(n=>n.headings!==1||n.math_warnings.length||n.images.some(i=>i.width===0)||n.overflow||n.raw_math.length)}
 await writeFile(join(out,'visual-'+(process.env.NOODLE_AUDIT_PASS||'baseline')+'.json'),JSON.stringify(report,null,2))
 console.log(JSON.stringify({totals:report.totals,issues:report.issues,responsive,errors},null,2))
}finally{await browser.close()}
