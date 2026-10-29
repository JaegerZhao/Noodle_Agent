"""Diagram-only HTML sources; all visible text belongs to a node or connector."""
from pathlib import Path
from html import escape
import json, subprocess, sys

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]
SKILL=ROOT/'.runtime/article-kit/diagram-design/skills/diagram-design'
FONT="'Geist', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', sans-serif"
INK='#2d3142'; MUTED='#4f5d75'; ACCENT='#bf4520'; PAPER='#ffffff'
figures=[]

def n(key,x,y,title,sub='',w=224,h=88,focal=False,store=False):
    return dict(id=key,x=x,y=y,w=w,h=h,title=title,sub=sub,focal=focal,store=store)

def e(a,b,*points,label='',label_box=None,dashed=False,d=''):
    return dict(source=a,target=b,points=points,label=label,label_box=label_box,dashed=dashed,d=d)

def rounded(points):
    if len(points)==2:return f'M {points[0][0]},{points[0][1]} L {points[1][0]},{points[1][1]}'
    commands=[f'M {points[0][0]},{points[0][1]}']
    for i in range(1,len(points)-1):
        a,b,c=points[i-1],points[i],points[i+1]
        da=abs(b[0]-a[0])+abs(b[1]-a[1]);dc=abs(c[0]-b[0])+abs(c[1]-b[1])
        r=min(8,da/2,dc/2)
        def unit(p,q):
            return ((q[0]-p[0])/max(1,abs(q[0]-p[0])),(q[1]-p[1])/max(1,abs(q[1]-p[1])))
        u,v=unit(a,b),unit(b,c)
        p=(b[0]-u[0]*r,b[1]-u[1]*r);q=(b[0]+v[0]*r,b[1]+v[1]*r)
        commands += [f'L {p[0]:g},{p[1]:g}',f'Q {b[0]},{b[1]} {q[0]:g},{q[1]:g}']
    commands.append(f'L {points[-1][0]},{points[-1][1]}')
    return ' '.join(commands)

def draw(slug,title,width,height,nodes,edges,zones=()):
    markers=''.join(f'<marker id="{name}" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="{color}"/></marker>' for name,color in [('arrow',MUTED),('arrow-accent',ACCENT),('arrow-link','#2e5aa8')])
    body=[f'<rect width="{width}" height="{height}" fill="{PAPER}"/>']
    for x,y,w,h in zones:
        body.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="none" stroke="#bfc0c0" stroke-width="0.8" stroke-dasharray="4,4"/>')
    for edge in edges:
        dash=' stroke-dasharray="5,4"' if edge['dashed'] else ''
        body.append(f'<path d="{edge["d"] or rounded(edge["points"])}" fill="none" stroke="{MUTED}" stroke-width="1.2" marker-end="url(#arrow)"{dash}/>')
        if edge['label']:
            x,y,w,h=edge['label_box']
            body.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" fill="{PAPER}"/>')
            body.append(f'<text x="{x+w/2}" y="{y+17}" font-family="{FONT}" font-size="12" text-anchor="middle" fill="{MUTED}">{escape(edge["label"])}</text>')
    for node in nodes:
        x,y,w,h=[node[k] for k in ('x','y','w','h')]
        color=ACCENT if node['focal'] else MUTED if node['store'] else INK
        fill='rgba(191,69,32,0.06)' if node['focal'] else '#f6f6f6' if node['store'] else PAPER
        body += [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{PAPER}"/>',f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{color}" stroke-width="1.2"/>']
        lines=node['title'].split('\n');subs=node['sub'].split('\n') if node['sub'] else []
        start=y+h/2-(len(lines)*24+len(subs)*20)/2+18
        for i,line in enumerate(lines):
            body.append(f'<text x="{x+w/2}" y="{start+i*24}" fill="{INK}" font-family="{FONT}" font-size="16" font-weight="600" text-anchor="middle">{escape(line)}</text>')
        for i,line in enumerate(subs):
            body.append(f'<text x="{x+w/2}" y="{start+len(lines)*24+i*20-2}" fill="{MUTED}" font-family="{FONT}" font-size="12" text-anchor="middle">{escape(line)}</text>')
    source=f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{margin:0;background:{PAPER}}}.diagram-container{{width:100%;overflow-x:auto}}svg{{display:block;width:100%;min-width:{width}px}}@media print{{.diagram-container{{overflow:visible}}svg{{min-width:0}}}}</style></head><body>
<div class="diagram-container"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="{slug}-title {slug}-desc"><title id="{slug}-title">{title}</title><desc id="{slug}-desc">{escape(title)}的组件、数据依赖和执行方向。</desc><defs>{markers}</defs>{''.join(body)}</svg></div></body></html>'''
    (OUT/(slug+'.html')).write_text(source,encoding='utf-8')
    mermaid='flowchart LR\n'+'\n'.join(f'    {v["id"]}["{v["title"].replace(chr(10)," / ")}{("（"+v["sub"].replace(chr(10)," / ")+"）") if v["sub"] else ""}"]' for v in nodes)+'\n'+'\n'.join(f'    {v["source"]} {"-.->" if v["dashed"] else "-->"}{("|"+v["label"]+"|") if v["label"] else ""} {v["target"]}' for v in edges)+'\n'
    (OUT/(slug+'.mmd')).write_text(mermaid,encoding='utf-8')
    subprocess.run([sys.executable,str(SKILL/'scripts/export_svg.py'),str(OUT/(slug+'.html')),'--system-fonts'],check=True,capture_output=True)
    figures.append(dict(slug=slug,title=title,width=width,height=height,nodes=nodes,edges=edges))

# Overall knowledge evolution: preserve the user's nine stages and publication fan-out.
nodes=[n('A',40,40,'PDF 上传与来源版本'),n('B',336,40,'MinerU 解析','或复用兼容缓存'),n('C',632,40,'类型识别与目录规划'),n('D',632,232,'必要视觉证据'),n('E',336,232,'Atomic 写作','逐节有限审查'),n('F',40,232,'发布 Atomic',focal=True),n('G',40,424,'Atomic 索引与浏览'),n('H',336,424,'Concept 归属与构建',focal=True),n('I',632,424,'Concept 索引','结构维护')]
edges=[e('A','B',(264,84),(336,84)),e('B','C',(560,84),(632,84)),e('C','D',(744,128),(744,232)),e('D','E',(632,276),(560,276)),e('E','F',(336,276),(264,276)),e('F','G',(152,320),(152,424)),e('F','H',(264,300),(296,300),(296,468),(336,468)),e('H','I',(560,468),(632,468))]
draw('01-knowledge-evolution','知识演化完整流程',896,552,nodes,edges)

nodes=[n('A',40,40,'PDF 与来源登记','稳定 source_id / 来源版本'),n('B',336,40,'解析并保存来源证据','正文块 · 公式 · 图片 · 页码'),n('C',632,40,'资料识别与主题规划','书籍分章；课件与论文聚合'),n('D',632,232,'按需视觉识别','原生文本不足或图表待理解'),n('E',336,232,'构建 Atomic 草稿','多小节 · 图文 · 来源分配',focal=True),n('F',40,232,'逐节审查与有限修复','Flash 一次审查 / 一次修复'),n('G',40,424,'版本校验与 Atomic 发布','正文、资产和证据一起提交',focal=True),n('H',336,424,'资料内笔记组织','书籍目录 / 小节顺序 / MOC'),n('I',632,424,'Atomic 索引构建','Chunk → Embedding → 索引代')]
edges=[e('A','B',(264,84),(336,84)),e('B','C',(560,84),(632,84)),e('C','D',(744,128),(744,232)),e('D','E',(632,276),(560,276)),e('E','F',(336,276),(264,276)),e('F','G',(152,320),(152,424)),e('G','H',(264,452),(336,452)),e('G','I',(152,512),(152,568),(744,568),(744,512))]
draw('02-atomic-generation','PDF 到 Atomic、笔记组织与索引',896,608,nodes,edges)

nodes=[n('A',40,40,'读取有效 Atomic','冻结版本、小节与来源角色'),n('B',336,40,'组装对象候选与上下文','名称 / 别名 / 已有绑定 / 核心覆盖'),n('C',632,40,'对象身份判断','Pro：复用、新建或待解决',focal=True),n('D',632,232,'分配小节贡献','新增 / 支持 / 阅读 / 上下文 / 冲突'),n('E',336,232,'计算目标状态','有效贡献签名与 generation'),n('F',40,232,'增量构建正文与媒体','复用已有核心；图片独立判断'),n('G',40,424,'审查并发布 Concept','来源、版本、目标代次与租约校验',focal=True),n('H',336,424,'Concept 向量与全文索引','仅纳入当前有效版本'),n('I',632,424,'知识结构整理','关系类型、方向与知识体系')]
edges=[e('A','B',(264,84),(336,84)),e('B','C',(560,84),(632,84)),e('C','D',(744,128),(744,232)),e('D','E',(632,276),(560,276)),e('E','F',(336,276),(264,276)),e('F','G',(152,320),(152,424)),e('G','H',(264,452),(336,452)),e('G','I',(152,512),(152,568),(744,568),(744,512)),e('I','H',(632,484),(560,484),label='再索引',label_box=(568,452,56,24),dashed=True)]
draw('03-concept-maintenance','Concept 归并、索引与结构维护',896,608,nodes,edges)

# Current architecture, including the unified DSH session path; no direct UI /answer path.
nodes=[n('W',40,56,'用户 / Web 工作台','阅读、上传与右侧助手',w=232),n('B',40,232,'FastAPI 会话桥','RPC 白名单 / scope / rpcId',w=232),n('H',368,56,'DeepSeek Harness','Agent Loop / Session',w=232),n('T',368,232,'17 个领域工具','13 读 / 4 个审批写操作',w=232,focal=True),n('P',368,408,'TypeScript HTTP Provider','统一状态、引用与追踪头',w=232),n('A',704,408,'FastAPI /internal/next','参数、范围和请求校验',w=232),n('D',1000,408,'Python 领域服务','知识演化 / RAG / 版本发布',w=232,focal=True),n('DB',1000,240,'SQLite','身份 / 版本 / 事件 / 任务 / 证据',w=232,store=True),n('FS',1000,584,'不可变正文与本地资产','PDF / Markdown / 检查点',w=232,store=True),n('Q',704,584,'独立 Worker','租约、心跳与阶段恢复',w=232),n('M',704,760,'解析与模型服务','MinerU / 文本 / 视觉 / Embedding',w=232),n('I',1000,760,'全文与向量索引','FTS5 / ExactVectors',w=232,store=True)]
edges=[e('W','B',(156,144),(156,232)),e('B','H',(272,276),(320,276),(320,100),(368,100),label='会话 RPC',label_box=(328,168,80,24)),e('H','T',(484,144),(484,232)),e('T','P',(484,320),(484,408)),e('P','A',(600,452),(704,452)),e('A','D',(936,452),(1000,452)),e('W','A',(40,100),(16,100),(16,528),(820,528),(820,496),label='资料与阅读请求',label_box=(392,496,128,24)),e('D','DB',(1116,408),(1116,328)),e('D','FS',(1116,496),(1116,584)),e('D','I',(1232,452),(1280,452),(1280,804),(1232,804)),e('DB','Q',d='M 1232,284 H 1240 Q 1248,284 1248,292 V 544 Q 1248,552 1240,552 H 1124 a 8,8 0 0,0 -16,0 H 828 Q 820,552 820,560 V 584',dashed=True,label='领取任务',label_box=(952,520,80,24)),e('Q','D',(936,628),(968,628),(968,476),(1000,476)),e('Q','M',(820,672),(820,760))]
draw('04-agent-architecture','完整项目与 DSH 架构',1320,888,nodes,edges)

nodes=[n('Q',40,120,'问题、范围与实体提示'),n('S',336,120,'索引快照与有效版本'),n('L',632,40,'关键词召回','FTS5 / BM25'),n('V',632,240,'向量召回','查询 Embedding / 余弦相似度'),n('R',928,140,'RRF 排名融合',focal=True),n('C',928,400,'可选重排与证据包','去重、邻域扩展与按笔记限额'),n('A',632,400,'生成引用回答','一次支持检查与有限修正',focal=True),n('G',336,400,'引用、结构与当前性检查'),n('O',40,400,'返回领域答案与引用','note / revision / section / PDF 页码')]
edges=[e('Q','S',(264,164),(336,164)),e('S','L',(560,144),(592,144),(592,84),(632,84)),e('S','V',(560,184),(608,184),(608,284),(632,284)),e('L','R',(856,84),(896,84),(896,164),(928,164)),e('V','R',(856,284),(880,284),(880,196),(928,196)),e('R','C',(1040,228),(1040,400)),e('C','A',(928,444),(856,444)),e('A','G',(632,444),(560,444)),e('G','O',(336,444),(264,444))]
draw('05-rag-retrieval','混合检索与引用回答',1192,528,nodes,edges)

nodes=[n('A',40,40,'删除、撤回或替换来源'),n('B',336,40,'更新当前有效资格','撤回时先过滤；恢复时重新核验',focal=True),n('C',632,40,'按贡献依赖重整 Concept','重算正文、媒体与关系支持'),n('D',632,232,'请求索引维护','合并待处理索引需求'),n('E',336,232,'重建当前有效 Chunk 集合','失效 Chunk 不进入新索引代'),n('F',40,232,'校验并激活新索引代','旧向量可留作历史，查询不再使用'),n('G',40,424,'恢复来源或笔记','重算依赖并重新索引')]
edges=[e('A','B',(264,84),(336,84)),e('B','C',(560,84),(632,84)),e('C','D',(744,128),(744,232)),e('B','D',(560,108),(592,108),(592,212),(684,212),(684,232)),e('D','E',(632,276),(560,276)),e('E','F',(336,276),(264,276)),e('G','B',(152,424),(152,372),(304,372),(304,164),(448,164),(448,128))]
draw('06-invalidation','删除、索引失效与恢复',896,552,nodes,edges)

nodes=[n('A',40,120,'当前有效 Atomic / Concept','读取当前发布版本'),n('B',336,120,'按小节编译 Chunk','范围、偏移、hash 与来源证据'),n('C',632,40,'生成全文检索词','标题 / 正文 → FTS5'),n('D',632,240,'复用向量或批量 Embedding','按 profile + 内容 hash 缓存'),n('E',928,240,'归一化 float32 向量','矩阵、缓存与校验文件'),n('F',928,40,'构建索引代与 manifest','全文投影 + 向量 + Chunk 元数据'),n('G',928,424,'发布前检查','来源资格、版本、profile 与覆盖',focal=True),n('H',632,424,'原子激活索引 head','查询使用同一索引快照',focal=True)]
edges=[e('A','B',(264,164),(336,164)),e('B','C',(560,144),(592,144),(592,84),(632,84)),e('B','D',(560,184),(608,184),(608,284),(632,284)),e('D','E',(856,284),(928,284)),e('C','F',(856,84),(928,84)),e('E','F',(1040,240),(1040,128)),e('F','G',(1152,84),(1200,84),(1200,468),(1152,468)),e('G','H',(928,468),(856,468))]
draw('07-index-build','全文与向量索引构建',1240,552,nodes,edges)
(OUT/'figures.json').write_text(json.dumps(figures,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(f'Generated {len(figures)} diagram-only HTML/SVG pairs and editable Mermaid sources.')
