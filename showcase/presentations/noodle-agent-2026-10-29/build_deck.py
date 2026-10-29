"""Windows-compatible, local-asset scaffold from html-ppt presenter-mode-reveal."""
from pathlib import Path
import html, json, re, shutil, hashlib

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]
SKILL=Path('C:/Users/Administrator/.agents/skills/html-ppt')
TEMPLATE=SKILL/'templates/full-decks/presenter-mode-reveal/index.html'

def prepare():
    for d in ('assets/themes','assets/animations','diagrams','screenshots','qa'):
        (OUT/d).mkdir(parents=True,exist_ok=True)
    for src,dest in [('assets/base.css','assets/base.css'),('assets/runtime.js','assets/runtime.js'),('assets/animations/animations.css','assets/animations/animations.css')]:
        shutil.copyfile(SKILL/src,OUT/dest)
    (OUT/'assets/fonts.css').write_text(":root{--font-sans:'Segoe UI','Microsoft YaHei UI','Microsoft YaHei',sans-serif;--font-display:var(--font-sans);--font-mono:'Cascadia Code','Consolas',monospace;}\n",encoding='utf-8')
    for path in (ROOT/'docs/project-introduction-assets-2026-10-29').glob('*.svg'):
        shutil.copyfile(path,OUT/'diagrams'/path.name)
    license=SKILL/'LICENSE'
    if license.exists():shutil.copyfile(license,OUT/'assets/LICENSE-html-ppt')
    return TEMPLATE.read_text(encoding='utf-8')

def frame(src,alt,klass=''):
    return f'<figure class="img-frame contain {klass}"><img src="{src}" alt="{html.escape(alt)}" loading="eager"></figure>'

def render(slide,i):
    kind=slide.get('layout','text')
    title=html.escape(slide['title'])
    chapter=html.escape(slide['chapter'])
    heading=f'<header class="slide-head"><span class="chapter-name">{chapter}</span><h2>{title}</h2></header>'
    if kind=='cover':
        body=slide['html']
    elif kind=='diagram':
        body=heading+'<div class="diagram-layout">'+frame('diagrams/'+slide['diagram'],slide['title'],'diagram-frame')+'<div class="diagram-rail">'+slide['html']+'</div></div>'
    elif kind=='screenshot':
        body=heading+'<div class="screen-layout">'+frame('screenshots/'+slide['image'],slide['alt'],'screen-frame')+'<div class="screen-caption">'+slide['html']+'</div></div>'
    elif kind=='compare-screens':
        body=heading+'<div class="comparison-images">'+''.join('<div>'+frame('screenshots/'+s['src'],s['label'])+'<h3>'+html.escape(s['label'])+'</h3></div>' for s in slide['images'])+'</div>'+slide['html']
    else:
        body=heading+'<div class="slide-body">'+slide['html']+'</div>'
    notes=''.join('<p>'+p+'</p>' for p in slide['notes'])
    variant=html.escape(slide.get('variant',''))
    return f'<section class="slide {kind} {variant}" id="{slide["id"]}" data-title="{title}" data-chapter="{chapter}" data-layout="{kind}" data-main="{str(i<22).lower()}">{body}<aside class="notes">{notes}</aside></section>'

def build():
    template=prepare()
    data_path=OUT/'slides.json'
    if not data_path.exists():
        print('Presenter template and local assets scaffolded; waiting for slides.json.')
        return
    data=json.loads(data_path.read_text(encoding='utf-8'))
    template=re.sub(r'(?:\.\./)+assets/','assets/',template)
    template=re.sub(r'data-themes="[^"]*"','data-themes="noodle"',template)
    template=re.sub(r'<title>.*?</title>','<title>Noodle Agent · 项目介绍</title>',template,count=1)
    template=template.replace('assets/themes/tokyo-night.css','assets/themes/noodle.css')
    template=template.replace('<link rel="stylesheet" href="style.css">','<link rel="stylesheet" href="style.css">\n<link rel="stylesheet" href="refined.css">')
    content='\n'.join(render(s,i) for i,s in enumerate(data['slides']))
    body=f'''<body class="tpl-presenter-mode-reveal noodle-deck"><div class="deck" data-w="1920" data-h="1080">
{content}
<div class="deck-footer"><a href="#/1" class="wordmark"><span class="brand-mark" aria-hidden="true"></span>Noodle Agent</a><span class="footer-chapter"></span><span class="slide-number"></span></div></div>
<nav class="deck-controls" aria-label="演示控制"><button type="button" data-key="ArrowLeft" aria-label="上一页">←</button><button type="button" data-key="ArrowRight" aria-label="下一页">→</button><span class="control-separator"></span><button type="button" data-key="o">总览 <kbd>O</kbd></button><button type="button" data-key="n">讲稿 <kbd>N</kbd></button><button type="button" data-key="s">演讲者 <kbd>S</kbd></button><button type="button" data-key="f">全屏 <kbd>F</kbd></button></nav>
<dialog id="image-dialog" aria-label="放大查看图片"><button class="close-image" type="button">关闭</button><img alt="放大图片"></dialog>
<script src="assets/runtime.js"></script><script src="deck.js"></script></body>'''
    template=re.sub(r'<body[\s\S]*?</body>',lambda _:body,template,count=1)
    def version_asset(match):
        key,path=match.groups()
        asset=OUT/path
        if asset.is_file():
            stamp=hashlib.sha256(asset.read_bytes()).hexdigest()[:10]
            return f'{key}="{path}?v={stamp}"'
        return match.group(0)
    template=re.sub(r'(href|src)="([^"?]+\.(?:css|js))"',version_asset,template)
    (OUT/'index.html').write_text(template,encoding='utf-8')
    # Pure Markdown notes are useful independently of popup support.
    notes=['# Noodle Agent：演示逐页讲稿\n']
    for i,s in enumerate(data['slides'],1):
        notes.append(f'## {i:02} · {s["chapter"]} · {s["title"]}\n')
        notes.extend(re.sub(r'<[^>]+>','',p)+'\n' for p in s['notes'])
    (OUT/'speaker-notes.md').write_text('\n'.join(notes),encoding='utf-8')
    print(f'Built {len(data["slides"])} slides with local diagrams and screenshots.')

if __name__=='__main__':build()
