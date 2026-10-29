"""Presentation content and oral notes, maintained separately from rendering."""
from pathlib import Path
import json
O=Path(__file__).resolve().parent
slides=[]
def add(title,chapter,html,notes,layout='text',**kw):
    slides.append(dict(id=f's{len(slides)+1:02}',title=title,chapter=chapter,html=html,notes=notes,layout=layout,**kw))
def diag(title,name,html,notes,chapter='03 / 知识演化'):
    add(title,chapter,html,notes,'diagram',diagram=name)
def shot(title,name,html,notes,chapter='03 / 知识演化'):
    add(title,chapter,html,notes,'screenshot',image=name,alt=title+'，项目工作台实际截图')

add('Noodle Agent','01 / 开场','''<div class="cover-line"></div><h1>Noodle Agent</h1><p class="lede">基于 Deepseek Harness 开发的可溯源本地个人知识管理 Agent，<br>重点解决多文档知识管理、知识持续演化和可靠问答问题。</p><div class="cover-flow">PDF <i>→</i> Atomic 笔记 <i>→</i> Concept 知识对象 <i>→</i> 引用问答</div><div class="cover-bottom">Workflow · 知识演化 · Agent 工具 · 检索溯源</div>''',[
'大家好，我介绍的项目是 <strong>Noodle Agent</strong>。它基于 DeepSeek Harness，面向个人长期积累的教材、课件和论文。用户上传资料以后，系统先生成可独立阅读、带来源的笔记，再把不同资料对同一知识对象的描述归并起来，支持后续更新和引用问答。',
'我主要实现了<strong>文档到知识的 Workflow、知识身份和贡献模型、版本证据、检索引用与任务恢复</strong>。本地指知识和任务状态保存在本地，解析和模型推理仍使用配置的外部服务。接下来先从实际痛点和我实现的知识流程讲起。'],'cover')
add('资料越积越多，知识却越来越难维护','02 / 项目背景','''<div class="problem-grid"><div class="problem"><h3>文档分散</h3><p>同一主题散落在教材、课件、论文中。</p></div><div class="problem"><h3>内容冗杂</h3><p>导航、重复背景与实质知识混在一起。</p></div><div class="problem"><h3>不便浏览</h3><p>跨页公式和图表难直接变成可读笔记。</p></div><div class="problem"><h3>知识重复</h3><p>新资料究竟是重复支持，还是内容增量？</p></div></div><p class="statement">目标：让知识能够长期整理、持续更新。</p>''',[
'做这个项目的起点是我学习同一主题时，经常要在<strong>教材、课件和论文</strong>之间来回切换。它们有重叠，也有各自独有的推导、图示和条件。文件越多，搜索和整理越费时间，而且一次问答结束以后，得到的知识未必沉淀成能继续维护的内容。',
'所以我把目标定为<strong>长期整理与更新</strong>：先让每份资料变成可读笔记，再判断跨资料的知识身份和增量。这样用户能直接阅读，加入新资料也能继续演化。下面这张图是完整的知识生成主线。'])
diag('从资料导入，到持续演化的知识','01-knowledge-evolution.svg','''<h3>先发布可读笔记</h3><p>Atomic 发布后即可阅读，索引和 Concept 维护分别推进。</p><h3>再维护跨资料对象</h3><p>新资料进入贡献链，版本变化沿依赖传播。</p>''',[
'从左到右看，系统先登记<strong>PDF 的来源版本</strong>，使用 MinerU 解析或经过校验的兼容缓存，再按资料类型规划主题。必要时补视觉证据，随后写作、有限审查并发布 Atomic。Atomic 发布是一个关键边界，用户这时就可以开始阅读。',
'后面分成两条派生路径：一条负责<strong>索引与浏览</strong>，另一条负责 Concept 归属、构建和结构维护。目录组织不是向量构建的前置条件，也不要求用户等到全部维护结束。要理解这套流程，先区分三个不同的数据粒度。'])
add('Atomic、Concept、Chunk 各自解决什么','03 / 知识演化','''<div class="three"><div class="block focal"><h3>Atomic</h3><p class="accent">一份资料内的完整主题</p><p>标题、摘要、多小节<br>公式图片、来源证据<br>稳定身份 + 不可变修订</p><p class="small">优化：独立阅读和来源内修订</p></div><div class="block"><h3>Concept</h3><p class="accent">跨资料的稳定知识对象</p><p>身份、别名、对象边界<br>内部主题、贡献和关系<br>多份 Atomic 共同支持</p><p class="small">优化：跨来源归并与持续演化</p></div><div class="block"><h3>Chunk</h3><p class="accent">已发布正文的检索范围</p><p>笔记、修订、小节 ID<br>起止偏移、文本 hash<br>证据与资产定位</p><p class="small">优化：召回精度和上下文长度</p></div></div><p class="rule-note">Atomic 与 Concept 都可以产生多个 Chunk；原 PDF 与目录地图不直接作为这条问答链路的事实语料。</p>''',[
'这里最容易混淆的是把它们都理解成切片。<strong>Atomic 是一份资料里的综合主题笔记</strong>，可以包含多个小节，跨多页保留完整推导。Concept 则代表跨资料长期维护的对象，比如支持向量机，内部再组织硬间隔、软间隔和核方法。',
'<strong>Chunk 才是检索范围</strong>，它从已发布的 Atomic 或 Concept 正文中切出，保存修订、小节和偏移。一个粒度服务阅读，一个服务知识演化，一个服务召回。下面先看 PDF 怎么变成 Atomic。'])
diag('PDF 如何生成 Atomic 笔记','02-atomic-generation.svg','''<h3>主题规划</h3><p>教材按可信目录分批；课件聚合连续主题；论文围绕方法与实验。</p><h3>带证据发布</h3><p>正文、引用和资产一起检查。阅读组织与索引分别派生。</p>''',[
'生成时先保留<strong>来源身份和来源版本</strong>，避免同名文件误命中缓存。解析结果带有正文块、标题、公式、图片和页码。规划不是一页生成一篇，而是按资料类型组织完整主题：教材参考目录，课件聚合同一对象，论文突出方法、实验和限制。',
'每个写作单元只带入相关证据和必要上下文，生成结构化小节后检查引用与资源，再发布不可变修订。<strong>note_id 保持稳定，revision_id 标识一次正文快照</strong>。接下来是这条流程里质量和成本之间的具体取舍。'])
add('在规划、写作与审查之间分配模型能力','03 / 知识演化','''<table><thead><tr><th>阶段</th><th>默认分工</th><th>约束</th></tr></thead><tbody><tr><td>主规划、排除项与标题组织</td><td><strong>Pro</strong></td><td>提前减少主题边界错误</td></tr><tr><td>Atomic 常规写作</td><td><strong>Flash</strong></td><td>明确主题、来源与输出结构</td></tr><tr><td>Section 审查与修复</td><td><strong>一次审查 + 一次定向修复</strong></td><td>修复后不无限循环语义复审</td></tr><tr><td>局部格式恢复</td><td>最多一次 Pro 恢复</td><td>限定失败范围</td></tr><tr><td>视觉理解</td><td>配置的视觉模型，按需调用</td><td>页图用于理解，发布配图另做资产校验</td></tr></tbody></table><p class="rule-note">审查不可用时保留警告；有限审查仍需要抽查与独立质量评测。</p>''',[
'模型分工里，<strong>Pro 和 Flash 是路由角色</strong>，实际模型看运行配置。前期规划一旦分错，后面写作和审查都会付出额外成本，所以优先把能力用在主题边界。常规写作已有较明确的输入约束，交给 Flash。视觉只在图表或解析缺失时使用。',
'审查采用<strong>一次 Flash 审查，有意见再修复一次</strong>。之后做结构、引用和资产检查，局部格式问题最多再恢复一次。这个选择降低循环修复成本，但不保证消除事实错误。下一页可以看到这些步骤最终形成的阅读界面。'])
shot('生成结果：可以直接阅读的主题笔记','01-atomic-reading.png','<p>目录、主题正文、公式与图片共同构成阅读界面。</p><p>实际工作台 · 16:9 重新截图</p>',[
'这页是本轮从<strong>实际工作台重新截取</strong>的画面。阅读视图把导航、正文和助手放在一起，用户可以沿目录浏览主题，也可以在正文里查看公式和图片。截图用于说明阅读体验，本轮没有重新执行知识生成，也不把画面当成生成质量的完整验收。',
'这里需要强调，前端目录是阅读投影，目录项不等于一定对应一篇 Atomic；笔记发布、索引就绪和后台知识维护也有不同状态。看完单份资料的结果，我们再看多份资料怎样共同维护一个 Concept。'])
diag('跨资料归并：先确定对象，再判断贡献','03-concept-maintenance.svg','''<h3>身份判断</h3><p>冻结 Atomic 版本，构造有界候选，判断复用、新建或待解决。</p><h3>增量维护</h3><p>按贡献和输入签名更新受影响主题，发布后触发索引与结构维护。</p>''',[
'Concept 的难点是<strong>对象身份和内容增量要分开判断</strong>。系统先读取当前有效 Atomic 并冻结版本，结合已有绑定、名称、别名、边界和核心覆盖构造候选，再判断应该复用、建立新对象，还是保留待解决状态。主批量归属并不是直接照搬问答的向量检索。',
'确定对象以后，再分配小节和内容贡献。签名不变就避免重复维护；有增量只更新受影响主题。发布还要重查来源、版本、目标代次和租约，防止旧任务覆盖新状态。接下来用贡献类型说明为什么新增资料不一定要重写正文。'])
add('新来源进入后，不一定重写 Concept','03 / 知识演化','''<table><thead><tr><th>贡献</th><th>正文与证据如何变化</th><th>SVM 示例</th></tr></thead><tbody><tr><td><strong>CORE_DELTA</strong></td><td>更新受影响主题</td><td>补充原来缺失的推导</td></tr><tr><td><strong>SUPPORT_ONLY</strong></td><td>增加支持，尽量复用正文</td><td>另一份课件解释相同定义</td></tr><tr><td>FURTHER_READING</td><td>增加深入阅读关联</td><td>更详细的方法说明</td></tr><tr><td>CONTEXT_ONLY</td><td>参与判断，不直接进入核心</td><td>邻近背景与术语解释</td></tr><tr><td>CONFLICT</td><td>保留条件差异和争议</td><td>适用前提或结论不一致</td></tr></tbody></table><p class="rule-note">文字与图片分别判断贡献；撤回一份来源后，仍有有效支持的对象应继续保留。</p>''',[
'这张表是知识演化里比较重要的设计。<strong>新增来源不等于新增正文</strong>。如果它补充了推导，就是核心增量；如果只是再次支持已有定义，就增加支持记录并尽量复用正文。深入阅读、判断上下文和冲突也有各自的处理方式。',
'例如 SVM 已经有教材和课件共同支持，删除课件以后，应重新计算剩余支持，而不是直接删除整个对象。图片还有独立贡献逻辑：文字没有变化，新资料仍可能提供更清晰的架构图。下一页结合实际工作台看知识对象和来源如何呈现。'])
shot('知识对象与来源支持共同呈现','02-concept-sources.png','<p>稳定知识对象，跨资料维护正文与来源支持。</p><p>实际工作台 · 16:9 重新截图</p>',[
'这页展示<strong>Concept 的实际阅读和来源入口</strong>。知识对象保持一个稳定身份，来源可以有多份。正文里的内部主题、来源支持以及知识体系中的显示位置，是不同层次的信息。一个对象出现在多个体系里，也不需要复制多份正文。',
'结构维护会处理父子、使用、比较和相关关系，已有边可以保留、移除或替换，但不能为了让图更连通，就把普通提及强行变成父子关系。当前结构主要服务浏览和维护，主问答仍采用混合检索。下面介绍承载这些能力的 Agent 架构。'])
diag('Agent 与知识服务如何连接','04-agent-architecture.svg','''<h3>DSH：会话与工具循环</h3><p>Web 助手和 Harness 共用原生 Agent Loop 与领域工具。</p><h3>Python：知识业务事实</h3><p>版本、证据、检索、任务和发布条件统一由领域服务维护。</p>''',[
'整体架构分成<strong>会话运行时和知识领域服务</strong>。Web 工作台的助手通过会话桥接进入 DSH 原生 Agent Loop，工具经过类型化契约和写操作审批，再通过统一的 TypeScript HTTP Provider 调用 FastAPI 的 internal/next 接口。',
'Python 服务维护知识、版本、证据和任务。SQLite 保存身份与状态，本地文件保存不可变正文、资产和检查点；独立 Worker 运行耗时步骤。选择 Harness，是复用成熟的循环、会话和审批机制，把主要实现精力放在这些领域能力上。'],'04 / Agent 框架')
add('复用 Harness，把精力集中在领域能力','04 / Agent 框架','''<div class="two"><div class="block"><h3>DeepSeek Harness</h3><p>模型调用与 Agent Loop<br>会话历史、工具分派<br>写操作审批<br>上下文压缩与工具输出裁剪</p></div><div class="block focal"><h3>我的主要实现</h3><p>PDF → Atomic → Concept Workflow<br>身份、版本、贡献与证据模型<br>混合检索和引用定位<br>Worker、租约、幂等与阶段恢复</p></div></div><p class="rule-note">工具返回当前业务状态；Agent 根据结果继续调用或结束当前回合。</p>''',[
'这里我会明确<strong>哪些是复用，哪些是自己实现</strong>。Harness 提供模型调用、会话历史、工具循环、审批以及上下文管理。我接入和设计领域工具，让这些工具访问可验证的知识状态，而不是让外层模型自己猜测任务是否完成。',
'我主要做的是知识 Workflow 和数据模型，以及围绕它们的检索、溯源和恢复。这样修改会话模型不会改变知识版本规则，修订笔记也必须经过领域服务。这个边界落实到实际接口，就是下一页的十七个工具。'])
add('17 个工具：把领域能力变成明确契约','04 / Agent 框架','''<div class="tool-groups"><div class="tool-group"><h3>检索与阅读</h3><p>引用问答、全文 / 向量检索<br>读取 Atomic 与 Concept</p></div><div class="tool-group"><h3>浏览与结构</h3><p>知识目录、资料、体系入口<br>关系与 Concept 列表</p></div><div class="tool-group"><h3>状态与任务</h3><p>就绪检查、来源进度<br>任务列表与详情</p></div><div class="tool-group"><h3>统一桥接</h3><p>13 个只读 + 4 个审批写工具<br>统一 TypeScript HTTP Provider</p></div></div><div class="write-tools"><strong>需要审批：</strong>修订小节 · 刷新知识结构 · 暂停任务 · 恢复任务</div>''',[
'当前工具总数是<strong>十七个，十三个只读、四个审批写</strong>。只读能力覆盖问答、搜索、笔记读取、目录关系以及任务状态。写工具包括修订小节、刷新结构、暂停和恢复任务。它们现在都走统一 HTTP Provider，不再有两套分散的业务访问方式。',
'设计工具时我更关注参数范围、返回状态和版本约束。例如先确认知识就绪，再做问答；读取目标小节以后，再提出修订。工具不能只返回一段自然语言成功提示。下面用小节编辑说明一次受控写操作。'])
add('一次小节编辑，经过版本与审批边界','04 / Agent 框架','''<div class="edit-flow"><div class="edit-step">读取目标<em>笔记 · 小节 · 当前修订</em></div><div class="edit-arrow">→</div><div class="edit-step">提出修改<em>范围明确的编辑内容</em></div><div class="edit-arrow">→</div><div class="edit-step">用户审批<em>确认实际写操作</em></div><div class="edit-arrow">→</div><div class="edit-step">领域服务校验<em>状态 · 版本 · 权限边界</em></div><div class="edit-arrow">→</div><div class="edit-step">发布新修订<em>触发索引与依赖维护</em></div></div><p class="statement">让模型提出操作，让领域规则决定能否发布。</p>''',[
'以<strong>编辑一个小节</strong>为例，Agent 需要先读取目标和当前修订，形成范围明确的修改，再进入写操作审批。审批通过以后，也不是直接覆盖正文，而是由领域服务检查当前状态和版本，发布新修订，并触发相应索引和依赖维护。',
'如果审批期间其他任务已经改变了输入，旧结果不能无条件落盘。这种设计把会话里的意图和知识库里的事实写入分开。暂停、恢复和结构刷新也遵循明确工具边界。说完工具，再看长对话和长期知识分别由谁保存。'])
add('会话记忆、工具上下文、长期知识分层管理','04 / Agent 框架','''<div class="memory-row"><b>会话历史</b><span>DSH 会话与压缩</span><span>保存对话过程，控制上下文长度</span></div><div class="memory-row"><b>工具结果</b><span>按需读取与输出裁剪</span><span>大结果通过明确入口继续读取</span></div><div class="memory-row"><b>长期知识</b><span>Python 领域存储</span><span>身份、修订、贡献、证据、索引</span></div><p class="rule-note">工具输出在上下文压力下可裁剪；知识正文与证据仍保存在领域存储中。</p>''',[
'项目里的记忆分成三层。第一层是<strong>会话历史</strong>，由 Harness 保存和压缩；第二层是工具结果，按需读取，较大的结果可以在上下文压力下裁剪；第三层才是长期知识，保存在领域服务管理的正文、版本、贡献和证据里。',
'所以对话压缩不会等同于删除知识。当前工具裁剪阈值预设为八千一百九十二字符，保留头尾，但要在上下文压力等条件下触发，不能说每次输出都会立即裁剪。接下来进入检索部分，看这些长期知识怎样变成可引用的回答。'])
diag('索引建立在当前有效的笔记修订上','07-index-build.svg','''<h3>全文与向量两条路径</h3><p>同一份可追踪 Chunk，分别支持词项召回和语义召回。</p><h3>配置与有效性约束</h3><p>Embedding profile 必须匹配；发布、修订和撤回驱动索引维护。</p>''',[
'索引的输入是<strong>当前有效的 Atomic 和 Concept 修订</strong>。系统按小节和正文范围生成 Chunk，保存笔记、版本、小节、偏移和文本 hash，再分别构建全文和向量索引。Chunk 不是丢掉来源以后的一段孤立文本。',
'当前记录的向量 profile 是 text-embedding-v4、一千零二十四维、ExactVectors，实际运行仍以配置和就绪结果为准。缓存和索引需要匹配 profile，不能混用。建立索引以后，问答会同时利用词项匹配和语义匹配。'],'05 / 检索与溯源')
diag('混合检索如何变成引用回答','05-rag-retrieval.svg','''<h3>先检查，再检索</h3><p>按就绪状态和来源范围选择全文与向量召回。</p><h3>回答携带定位</h3><p>融合去重、组织证据，再生成可返回笔记与 PDF 的引用。</p>''',[
'一次问答先检查<strong>知识就绪状态和来源范围</strong>。全文检索适合明确术语，向量召回补充语义相近内容，之后融合、去重并组织有界证据。回答必须依据符合当前有效性条件的片段，不能无条件使用历史索引命中的文本。',
'在 DSH 中，普通回答可以调用 answer 工具完成这条链路，工具结果再交给 Harness 输出。非 READY 时应说明状态并结束；继续任务需要明确意图。已有验收覆盖过 HYBRID READY 等情形，本轮主要验证展示。下一页看引用如何返回 PDF。'],'05 / 检索与溯源')
shot('从知识笔记返回原 PDF 证据','03-pdf-provenance.png','<p>引用 → 笔记修订 / 小节 → 来源版本 → PDF 页码。</p><p>实际工作台 · 16:9 重新截图</p>',[
'这页展示<strong>原 PDF 的定位入口</strong>。引用先关联到笔记修订、小节和正文范围，再沿来源证据找到对应 PDF 版本和页码。证据里还保留原件 hash，用来区分同一资料的不同版本。用户可以返回原文核对生成笔记的说法。',
'这条链路能帮助查证，但引用存在不等于陈述一定正确，仍要核对证据是否真正支持结论，尤其是公式条件和跨来源归纳。可靠问答也依赖删除和修订后的索引有效性，下面接着讲状态变化怎样传播。'],'05 / 检索与溯源')
diag('撤回内容后，先阻止旧知识继续被召回','06-invalidation.svg','''<h3>先更新有效性</h3><p>旧笔记和旧 Chunk 退出当前问答资格。</p><h3>再更新派生结果</h3><p>重算贡献、维护 Concept 和索引；历史文件与缓存可按策略保留。</p>''',[
'删除不能只理解为删掉一个文件。首先要让<strong>笔记和对应 Chunk 失去当前检索资格</strong>，阻止旧知识继续进入回答，再重建或更新索引。旧向量、正文和缓存可能作为历史保留，所以不能把这里描述成所有副本马上物理清除。',
'同时，系统沿贡献依赖重新计算 Concept。如果还有其他有效来源支持，就维护剩余内容；输入发生变化，旧任务的发布条件也会失效。这解决的是业务有效性的一致性。接下来讲独立 Worker 如何在中断之后继续推进。'],'06 / 任务可靠')
add('任务中断后，从可信阶段恢复','06 / 任务可靠','''<div class="timeline"><div class="time-step active"><h3>持久任务</h3><p>事件与任务进入 SQLite<br>HTTP 请求可先返回</p></div><div class="time-step"><h3>租约领取</h3><p>Worker 领取与续租<br>提交校验租约和代次</p></div><div class="time-step"><h3>幂等推进</h3><p>重复事件可安全处理<br>签名不变避免重复发布</p></div><div class="time-step"><h3>阶段恢复</h3><p>检查产物和 profile<br>从可信检查点继续</p></div></div><p class="statement">暂停、失败、进程退出，都要有可解释的任务状态。</p><p class="small">检查点存在仍需校验；恢复沿用冻结的任务 profile，避免新旧策略混用。</p>''',[
'耗时流程交给<strong>独立 Worker</strong>，HTTP 不需要一直等解析和生成完成。Worker 领取租约并续租，提交时核对租约、输入版本和目标代次。这样旧执行者即使晚返回，也不能覆盖当前结果。重复事件则通过幂等键和输入签名减少重复发布。',
'恢复时使用阶段产物和检查点，但文件存在不代表可信，还要核对来源、配置与冻结 profile。暂停、失败、进程退出都需要能解释的状态。最后看成本：我主要从并发调度、模型路由和上下文约束三个方向优化。'])
add('把高成本能力用在最影响后续的环节','07 / 成本优化','''<div class="three"><div class="block focal"><h3>并行与复用</h3><p>独立单元受控并发<br>兼容解析 / 视觉缓存<br>签名不变跳过维护</p></div><div class="block"><h3>模型选择</h3><p>关键规划使用 Pro<br>常规写作审查用 Flash<br>视觉与恢复按需调用</p></div><div class="block"><h3>输入输出约束</h3><p>只带相关证据与上下文<br>限制候选和输出结构<br>审查修复次数有界</p></div></div><div class="two"><div><div class="metric">−40.5%</div><div class="metric-label">文本 token：996,515 → 592,536</div></div><div><div class="metric">−32.4%</div><div class="metric-label">文本调用：185 → 125</div></div></div><p class="small muted">历史固定四份 PDF 对照记录；不等同于通用降幅、费用降幅或当前质量 A/B 结论。</p>''',[
'成本优化主要有三类。<strong>并行和缓存</strong>减少重复等待与重复计算，但共享发布状态仍要受控。模型按操作分工，让规划承担较强模型成本，常规写作与审查使用 Flash。输入只给相关证据，候选和输出结构也要有边界。',
'历史固定四份 PDF 的对照里，文本 token 从九十九万多降到五十九万多，减少约百分之四十点五；调用数从一百八十五到一百二十五。<strong>这是特定历史样本</strong>，不能直接推广成费用或质量结论。下一步仍要把质量和合并规则评测补齐。'])
add('继续优化知识质量与合并规则，再扩展输入','08 / 问题与扩展','''<div class="roadmap"><div><h3>知识质量</h3><p>覆盖与遗漏<br>公式、条件和图像证据<br>引用是否真正支持结论</p><p class="small">抽样审查 + 独立质量评测</p></div><div><h3>合并规则</h3><p>同一对象与内部变体<br>使用关系与对象边界<br>重复支持、冲突与作者修订</p><p class="small">可复现案例 + 合并回归集</p></div><div class="future"><h3>音视频输入</h3><p>转写与时间戳<br>关键帧和说话片段<br>复用 Atomic / Concept 演化链</p><p class="small">后续方向，尚未实现</p></div></div><p class="statement">让新增内容有去向，让每次更新有依据。</p>''',[
'当前还有两块需要继续打磨。第一是<strong>知识质量</strong>，包括遗漏、公式条件、图像证据和引用支持程度。第二是合并规则，怎样稳定地区分同一对象、内部变体和使用关系，怎样处理冲突和作者修订，需要更多可复现案例和回归评测。',
'后续我想扩展到<strong>视频和音频的笔记识别</strong>：先把时间戳、转写和关键帧作为新证据，再接入已有 Atomic、Concept 和版本维护链路。这部分目前是规划。项目当前最有价值的积累，是把生成、更新和查证连接成可持续维护的流程。'])
add('只读工具清单','附录 / 工具契约','''<table class="compact-table"><thead><tr><th>类别</th><th>工具</th><th>用途</th></tr></thead><tbody><tr><td>问答 / 检索</td><td class="term">answer · search · get_readiness</td><td>引用回答、召回、就绪状态</td></tr><tr><td>读取笔记</td><td class="term">get_atomic · get_concept</td><td>正文与目标对象</td></tr><tr><td>知识浏览</td><td class="term">list_concepts · list_knowledge<br>get_relations · list_knowledge_roots</td><td>对象、目录、关系与体系</td></tr><tr><td>来源 / 任务</td><td class="term">list_sources · get_source_status<br>list_tasks · get_task</td><td>来源状态、任务列表与详情</td></tr></tbody></table><p class="rule-note">共 13 个只读工具，统一通过 HTTP Provider 调用 Python 领域接口。</p>''',[
'这页是用于追问的<strong>只读工具完整清单</strong>。问答和搜索分开，读取完整对象和浏览目录也分开，避免每一次请求都返回过大的上下文。get_readiness 让 Agent 先了解当前索引和知识是否可用，任务与来源工具则提供可解释的进度。',
'这些工具共同走统一的 TypeScript HTTP Provider，业务状态仍由 Python 判断。工具名称本身不代表返回数据足够，参数、范围、版本和结构化状态才是契约的核心。涉及改变状态的能力放在下一页，单独经过审批。'])
add('四个写工具都需要审批','附录 / 工具契约','''<table><thead><tr><th>工具</th><th>动作</th><th>边界</th></tr></thead><tbody><tr><td class="term">edit_section</td><td>修订指定小节</td><td>明确笔记、小节、当前版本与修改范围</td></tr><tr><td class="term">refresh_knowledge_map</td><td>请求知识结构维护</td><td>提交维护任务，由领域规则校验更新</td></tr><tr><td class="term">pause_task</td><td>暂停指定任务</td><td>以持久任务状态为准</td></tr><tr><td class="term">resume_task</td><td>恢复指定任务</td><td>校验可恢复状态与冻结 profile</td></tr></tbody></table><p class="rule-note">审批通过后仍执行领域校验；任务提交成功不等于后台维护已经完成。</p>''',[
'这四个工具覆盖<strong>小节编辑、结构维护以及任务控制</strong>。它们都经过写操作审批，但审批只是确认意图，后面仍需要领域服务核对版本和当前状态。工具应明确返回已发布、已提交任务或无法执行，而不能把请求受理都写成操作完成。',
'暂停和恢复尤其需要区分任务记录与进程执行。恢复沿用原任务冻结的配置，检查已有阶段结果，再继续运行。这样外层对话保持简单，内部业务约束仍然完整。面试追问时，我会结合具体工具参数和任务状态进一步解释。'])
shot('后台任务：生成与维护状态分开观察','04-task-status.png','<p>任务进度与知识就绪状态共同用于判断当前可用能力。</p><p>实际工作台 · 16:9 重新截图</p>',[
'这里补充<strong>任务状态的实际画面</strong>。笔记生成、索引和跨资料维护有各自进度，不能只看一个总体完成图标。用户应当知道当前可以阅读什么、哪些内容能够参与检索，以及后台还有哪些工作没有结束。',
'截图用于展示状态呈现，不代表本轮重新跑过完整恢复验收。关于 Worker、租约和阶段恢复的结论来自当前源码和已有记录。后续仍需要把异常注入、恢复轨迹和最终发布结果整理成稳定回归用例，方便验证真实中断场景。'],'附录 / 任务状态')
add('实现范围与验证边界','附录 / 事实边界','''<table><thead><tr><th>已具备的链路</th><th>仍需持续验证</th><th>未来扩展</th></tr></thead><tbody><tr><td>PDF → Atomic → Concept<br>版本 / 贡献 / 证据维护<br>DSH 原生循环 + 17 工具<br>混合检索与来源定位<br>持久任务和阶段恢复</td><td>真实内容覆盖与事实质量<br>对象边界和冲突合并规则<br>不同异常下的恢复一致性<br>跨来源归纳和引用支持<br>UI 与引用交互回归</td><td>音频转写与片段证据<br>视频关键帧与时间戳<br>统一多模态笔记识别</td></tr></tbody></table><p class="rule-note">说明依据：2026-10-29 项目介绍、当前代码和已有验收记录；本轮重新截图并验证 HTML 演示，不重新执行模型生成评测。</p>''',[
'最后把<strong>事实边界</strong>集中放在这里。已经具备的是 PDF 到笔记和知识对象的链路、版本证据维护、DSH 工具接入，以及检索和任务恢复机制。需要继续验证的是内容质量、复杂合并、异常恢复一致性和引用交互。音视频输入仍是后续计划。',
'本次演示以十月二十九日项目介绍、当前代码和已有验收记录为依据；我们重新截取界面并验证演示文件，没有重新执行全量模型评测。因此展示实现能力时，我会把设计、已有验证和未来目标分别说清楚，避免把计划或历史样本当成当前完整结论。'])
from polish_content import polish
polish(slides)
(O/'slides.json').write_text(json.dumps({'title':'Noodle Agent','slides':slides},ensure_ascii=False,indent=2),encoding='utf-8')
print(f'Wrote {len(slides)} slides')
