// Continuous PDF pages with lazy rendering and a bounded canvas window.
let libraryPromise=null,cached=null,disposeCurrent=null,libraryAttempt=0;
async function pdfLibrary(){if(!libraryPromise)libraryPromise=import('/app/static/vendor/pdfjs/pdf.min.mjs?v=4.10.38&attempt='+(++libraryAttempt)).then(pdf=>{pdf.GlobalWorkerOptions.workerSrc='/app/static/vendor/pdfjs/pdf.worker.min.mjs?v=4.10.38';return pdf;}).catch(error=>{libraryPromise=null;throw error;});return libraryPromise;}
async function documentFor(url){
 if(cached?.url===url)return cached.promise;
 if(cached){cached.controller.abort();if(cached.task)void cached.task.destroy().catch(()=>{});}
 const item={url,controller:new AbortController(),task:null,promise:null};cached=item;
 item.promise=(async()=>{
  const pdf=await pdfLibrary();
  // Match the existing workbench's binary loading path. A single revision is
  // cached, avoiding plugin downloads and browser-specific Range interception.
  const response=await fetch(url,{headers:{Accept:'application/pdf'},signal:item.controller.signal});
  if(!response.ok)throw Error('原始 PDF 响应 '+response.status);
  const data=new Uint8Array(await response.arrayBuffer());if(item.controller.signal.aborted)throw new DOMException('PDF load cancelled','AbortError');
  item.task=pdf.getDocument({data});return item.task.promise;
 })().catch(error=>{if(cached===item)cached=null;throw error;});return item.promise;
}
export function clearPdfViewer(){disposeCurrent?.();disposeCurrent=null;}
export function mountPdfReader(pane,{url,page=1,el,action,onLocation=()=>{}}){
 clearPdfViewer();let alive=true,pdf=null,current=page,observer=null,timer=null,frame=0,epoch=0,running=0,lastWidth=0;const shells=[],queue=[],queued=new Set(),inFlight=new Set(),tasks=new Map();
 const toolbar=el('header',undefined,'pdf-toolbar'),view=el('div',undefined,'pdf-canvas-view'),status=el('span','正在载入 PDF…','pdf-reader-status');status.setAttribute('role','status');view.tabIndex=0;view.setAttribute('aria-label','原始 PDF 连续阅读');
 const previous=action('上一页',()=>goTo(current-1)),next=action('下一页',()=>goTo(current+1));previous.setAttribute('aria-label','PDF 上一页');next.setAttribute('aria-label','PDF 下一页');
 const label=el('label','页码 '),input=el('input'),total=el('span');input.type='number';input.min='1';input.step='1';input.value=String(page);input.setAttribute('aria-label','PDF 页码');label.append(input);input.onchange=()=>goTo(Number(input.value));input.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();goTo(Number(input.value));}};
 const download=el('a','下载 PDF');download.href=url;download.target='_blank';download.rel='noopener noreferrer';download.setAttribute('download','');toolbar.append(previous,label,total,next,status,download);pane.replaceChildren(toolbar,view);
 function update(){input.value=String(current);previous.disabled=!pdf||current<=1;next.disabled=!pdf||current>=pdf.numPages;input.disabled=!pdf;if(pdf)status.textContent='第 '+current+' 页 / 共 '+pdf.numPages+' 页 · 向下滚动连续阅读';}
 function evict(){for(const shell of shells){const number=Number(shell.dataset.pdfPage);if(Math.abs(number-current)>4&&shell.querySelector('canvas')&&!tasks.has(number)){shell.replaceChildren();delete shell.dataset.rendered;}}}
 function enqueue(number){const shell=shells[number-1];if(!alive||!shell||shell.dataset.rendered||queued.has(number)||inFlight.has(number))return;queued.add(number);queue.push(number);pump();}
 function pump(){while(alive&&running<2&&queue.length){const number=queue.shift();queued.delete(number);if(Math.abs(number-current)>5)continue;running++;inFlight.add(number);void paint(number).finally(()=>{inFlight.delete(number);running--;if(alive&&shells[number-1]?.dataset.retryResize==='true'){delete shells[number-1].dataset.retryResize;enqueue(number);}pump();});}}
 async function paint(number){const shell=shells[number-1],ticket=epoch;try{
  const pageProxy=await pdf.getPage(number);if(!alive||ticket!==epoch)return;const width=Math.max(200,view.clientWidth-24),base=pageProxy.getViewport({scale:1}),scale=width/base.width,density=Math.min(devicePixelRatio||1,2),viewport=pageProxy.getViewport({scale:scale*density});const location=getLocation(),ratio=base.width+' / '+base.height,changed=shell.style.aspectRatio!==ratio;shell.style.aspectRatio=ratio;if(changed&&!pane.hidden)restoreLocation(location);
  const canvas=el('canvas');canvas.width=Math.floor(viewport.width);canvas.height=Math.floor(viewport.height);canvas.style.width='100%';canvas.style.height='auto';canvas.setAttribute('role','img');canvas.setAttribute('aria-label','原始 PDF 第 '+number+' 页');shell.replaceChildren(canvas);
  const task=pageProxy.render({canvasContext:canvas.getContext('2d'),viewport,background:'rgb(255,255,255)'});tasks.set(number,task);await task.promise;if(!alive||ticket!==epoch)return;shell.dataset.rendered='true';canvas.dataset.page=String(number);
 }catch(error){if(alive&&ticket===epoch&&error.name!=='RenderingCancelledException'){shell.replaceChildren(el('p','第 '+number+' 页渲染失败。','status-error'),action('重试',()=>enqueue(number)));}}finally{tasks.delete(number);evict();if(alive&&ticket!==epoch&&Math.abs(number-current)<=1)shell.dataset.retryResize='true';}}
 let pending={page,ratio:0};
 function getLocation(){const shell=shells[current-1];return {page:current,ratio:shell?Math.max(0,(view.getBoundingClientRect().top+12-shell.getBoundingClientRect().top)/shell.clientHeight):0};}
 function restoreLocation(location){pending=location;if(!pdf)return;goTo(location.page);const shell=shells[current-1];view.scrollTop+=Math.max(0,Math.min(1,location.ratio||0))*shell.clientHeight;onLocation(current);}
 function goTo(number){pending={page:number,ratio:0};if(!alive||!pdf)return;if(!Number.isInteger(number)||number<1||number>pdf.numPages){input.setCustomValidity('请输入 1 到 '+pdf.numPages+' 之间的页码');input.reportValidity();return;}input.setCustomValidity('');current=number;update();const shell=shells[number-1];view.scrollTop+=shell.getBoundingClientRect().top-view.getBoundingClientRect().top-12;enqueue(number);enqueue(number+1);enqueue(number-1);evict();onLocation(current);}
 function sync(){if(!alive||!pdf||pane.hidden)return;const marker=view.getBoundingClientRect().top+20;let number=1;for(const shell of shells){if(shell.getBoundingClientRect().top<=marker)number=Number(shell.dataset.pdfPage);else break;}current=number;update();enqueue(current);enqueue(current+1);enqueue(current-1);evict();onLocation(current);}
 view.addEventListener('scroll',()=>{if(frame)return;frame=requestAnimationFrame(()=>{frame=0;sync();});},{passive:true});
 const resize=new ResizeObserver(()=>{const width=view.clientWidth;if(!pdf||pane.hidden||!width||Math.abs(width-lastWidth)<12)return;const location=getLocation();clearTimeout(timer);timer=setTimeout(()=>{lastWidth=width;epoch++;for(const task of tasks.values())task.cancel();for(const shell of shells){shell.replaceChildren();delete shell.dataset.rendered;}restoreLocation(location);},120);});resize.observe(view);
 update();documentFor(url).then(async value=>{if(!alive)return;pdf=value;input.max=String(pdf.numPages);total.textContent='/ '+pdf.numPages;const first=await pdf.getPage(1);if(!alive)return;const base=first.getViewport({scale:1});lastWidth=view.clientWidth;const fragment=globalThis.document.createDocumentFragment();for(let i=1;i<=pdf.numPages;i++){const shell=el('section',undefined,'pdf-page-shell');shell.dataset.pdfPage=String(i);shell.style.aspectRatio=base.width+' / '+base.height;shell.setAttribute('aria-label','PDF 第 '+i+' 页');shells.push(shell);fragment.append(shell);}view.append(fragment);
  observer=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting)enqueue(Number(entry.target.dataset.pdfPage));},{root:view,rootMargin:'700px 0px'});for(const shell of shells)observer.observe(shell);restoreLocation({...pending,page:Math.max(1,Math.min(pending.page,pdf.numPages))});
 }).catch(error=>{if(!alive)return;status.textContent='PDF 加载失败：'+error.message;status.setAttribute('role','alert');view.append(action('重新加载',()=>mountPdfReader(pane,{url,page,el,action})));});
 disposeCurrent=()=>{alive=false;epoch++;for(const task of tasks.values())task.cancel();observer?.disconnect();resize.disconnect();clearTimeout(timer);cancelAnimationFrame(frame);};
 return {goTo,getLocation,restoreLocation,get currentPage(){return current;}};
}
