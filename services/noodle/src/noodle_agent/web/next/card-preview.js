import {renderMarkdown} from './markdown.js';

export function renderCardPreview(target,source){
 renderMarkdown(target,source,{imageSource:()=>null});
 // Cards already act as one link. Preserve emphasis/math without nested links.
 for(const link of target.querySelectorAll('a'))link.replaceWith(...link.childNodes);
 for(const control of target.querySelectorAll('input,button,iframe,video,audio'))control.remove();
}
