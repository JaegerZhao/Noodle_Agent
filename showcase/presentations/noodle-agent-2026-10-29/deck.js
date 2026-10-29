/* Small authored controls; navigation and presenter sync stay in html-ppt runtime. */
document.addEventListener('DOMContentLoaded',()=>{
 const slides=[...document.querySelectorAll('.deck > .slide')];
 const syncSlide=()=>{
  const current=slides.find(slide=>slide.classList.contains('is-active'));
  slides.forEach(slide=>{const active=slide===current;slide.inert=!active;slide.setAttribute('aria-hidden',String(!active));});
  document.querySelector('.footer-chapter').textContent=current?.dataset.chapter.split(' / ').slice(1).join(' / ')||'';
 };
 new MutationObserver(syncSlide).observe(document.querySelector('.deck'),{subtree:true,attributes:true,attributeFilter:['class']});
 syncSlide();
 document.querySelectorAll('.overview .mini-slide > .slide').forEach((clone,i)=>{clone.className=slides[i].className.replace(/\bis-prev\b/g,'')+' is-active';clone.style.removeProperty('padding');});
 let controlsTimer;
 document.addEventListener('pointermove',()=>{document.body.classList.add('controls-awake');clearTimeout(controlsTimer);controlsTimer=setTimeout(()=>document.body.classList.remove('controls-awake'),1600)},{passive:true});
 const dialog=document.querySelector('#image-dialog');
 document.querySelectorAll('[data-key]').forEach(button=>button.addEventListener('click',()=>document.dispatchEvent(new KeyboardEvent('keydown',{key:button.dataset.key,bubbles:true}))));
 document.querySelectorAll('.deck .img-frame img').forEach(image=>{
  image.tabIndex=0;
  image.title='点击放大查看';
  image.addEventListener('click',()=>{dialog.querySelector('img').src=image.src;dialog.querySelector('img').alt=image.alt;dialog.showModal()});
  image.addEventListener('keydown',event=>{if(event.key==='Enter'){event.stopPropagation();image.click()}});
 });
 dialog.querySelector('.close-image').addEventListener('click',()=>dialog.close());
 dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close()});
 document.addEventListener('keydown',event=>{if(dialog.open){event.stopImmediatePropagation();if(event.key==='Escape')dialog.close()}},true);
});
