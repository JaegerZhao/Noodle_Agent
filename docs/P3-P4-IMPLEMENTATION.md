# P3 / P4 使用与实现

更新：2026-10-21。本页描述已实现的新系统后端；默认工作台仍运行旧系统。完整新前端、MOC 自动编排、Vault 和切换属于后续阶段。

## 本次完成的链路

```mermaid
flowchart LR
  A[P2 发布原子笔记] --> E[统一事件路由]
  E --> D[逐片段判断身份、章节和增量]
  D --> C[当前有效贡献清单]
  C --> W[复制未变章节 / 综合受影响章节]
  W --> Q[检查、审查、租约和版本屏障]
  Q --> P[发布 Concept Revision]
  P --> I[索引构建请求]
  E --> I
  I --> G[不可变索引代次]
  G --> F[FTS5 + 向量并行召回]
  F --> R[RRF / 可选重排 / 章节补全]
  R --> B[引用回答及最终有效性检查]
```

### Concept 维护

- 原子正文按章节切成完整语义片段，冻结版本、字符范围、SHA-256 和证据。每个片段均有处理记录，不设“每篇最多三个知识点”的限制。
- 模型先判断主题身份，再匹配章节，最后判断新增内容；独立审查器复核。已预留而未发布的 Concept 章节也进入目录，后续片段使用其真实 ID。
- 新信息可以创建 Concept、创建章节或更新章节；重复描述只补充证据，新版本正文保持一致且不调用 Writer；长案例保留为到原子笔记的链接。`INSUFFICIENT` 留下跳过原因，`CONFLICT` 停止并等待复核。重复处理相同片段由幂等账本直接跳过，不让模型用 `NO_CHANGE` 丢弃新支持。
- Writer 只看受影响章节的全部有效原子片段。未变章节由程序复制，不让模型重写全篇。重要知识、公式和条件通过结构、数学与独立模型审查后，提交新的完整 Revision。
- 删除或替换来源、替换或撤回原子笔记后，相关旧 Concept 立即失去检索资格；后台只按当前有效输入重建。全部支持消失则撤回正文；恢复来源时保留 Concept ID，重新发布有效版本。
- 人工保护的章节无法自动改写或删除。人工确认冲突使用决策哈希做 CAS，保留审计产物；接受后仍须等新正文发布，旧版才解除屏障。

`ConceptInput` 保存精确选段。章节仍属于 NoteRevision，不增加独立章节发布系统。当前每个原子片段选择一个主要目标；同一片段贡献多个 Concept 的多目标计划尚未实现。

### 检索与回答

语料只有当前有效的 `ATOMIC` 和 `CONCEPT`，不检索原 PDF、解析块、MOC 或聊天历史。页码是追溯定位，不会被拿来补充正文没有的事实。

Chunk 在不可变 Markdown 上保存字符范围和哈希，默认约 400 个 `cl100k_base` token；公式与前后解释、表格和代码块整体保留。异常大的单块进入复核，不固定截断。该 token 计量用于预算，并非 DeepSeek 的原生 tokenizer。

关键词路由用 SQLite FTS5 的 BM25，中文建立字/二元词投影；向量路由使用同一个问题向量，两路各召回最多 50 个候选，RRF 常数为 60。可选模型重排最多 30 个候选，再做同范围去重、每篇最多 3 段、短章节补全和 6,000 token 上下文预算，预算计入片段及来源元数据。

向量默认是归一化 float32 二进制缓存与代次矩阵，使用内存映射和批量点积。SQLite 保存领域事实、索引元数据和 FTS；不把向量 JSON 逐条解析。可选 Chroma HTTP 后端管理向量检索，仍以 SQL 的来源和活动版本判定资格。

新代次完全构建后才 CAS 切换活动指针。构建失败保留上一代；上一代中的过时 Revision 仍会被 SQL 屏障排除。模型、维度、归一化或输入政策改变后使用另一 profile，不混用旧向量。Embedding 缺失时拒绝 HYBRID；只有明确选择 KEYWORD_ONLY 才运行纯关键词模式。

生成回答时只允许引用检索快照中的 K 编号，独立审查每项结论的支持情况，最终再次核对实际引用版本。生成期间来源变化则返回 `CONTENT_CHANGED`，清空回答、命中正文和引用。首版缓冲完整回答再返回。

## 运行

在仓库根目录执行，Python 环境须已安装本项目。真实模型沿用 `.env` 的 DeepSeek 和 Embedding 配置。可先查看命令，不会触发外部请求：

```powershell
python -m noodle_agent.cli --help
python -m noodle_agent.cli next-schema
```

新资料的流程使用两个进程：

```powershell
# 已有新系统数据目录可直接复用；不要指向旧库。
python -m noodle_agent.cli next-init --data-dir ./data/noodle-next
python -m noodle_agent.cli next-ingest --data-dir ./data/noodle-next --pdf ./example.pdf

# 终端一：P2 PDF -> 原子笔记，逐任务运行。
python -m noodle_agent.cli next-worker --data-dir ./data/noodle-next --once

# 终端二：P3 归并与 P4 索引；持续运行，不负责重新生成原子笔记。
python -m noodle_agent.cli next-maintain --data-dir ./data/noodle-next

# 独立只读查询 API，提供 /docs。
python -m noodle_agent.cli next-api --data-dir ./data/noodle-next --port 8002
```

默认持续运行；需要限制检查次数时设置 `--max-steps`。`--once` 执行一个任务，`--until-idle` 排空当前可执行任务。任务达到复核状态会保留检查点，不会无限自动重试。

P2 已有笔记时可以登记后续处理，**不重跑 PDF 生成**：

```powershell
python -m noodle_agent.cli next-bootstrap --data-dir ./data/noodle-next
python -m noodle_agent.cli next-maintain --data-dir ./data/noodle-next --until-idle
python -m noodle_agent.cli next-ask --data-dir ./data/noodle-next --query "梯度下降与反向传播分别解决什么问题？"
python -m noodle_agent.cli next-search --data-dir ./data/noodle-next --query "反向传播" --source-id SOURCE_ID
```

`bootstrap --note-id ID` 仅登记指定笔记；维护器仍会处理库内其他已排队事件。登记和维护会调用真实模型进行归并；不代表重新生成原子笔记。首次处理现有资料可先在独立数据副本验收。

如未配置向量服务，显式使用关键词模式：

```powershell
python -m noodle_agent.cli next-maintain --data-dir ./data/noodle-next --until-idle --mode KEYWORD_ONLY
python -m noodle_agent.cli next-ask --data-dir ./data/noodle-next --query "反向传播" --mode KEYWORD_ONLY
```

API 默认请求模式仍是 HYBRID。请求纯关键词 API 时应在 JSON 里传 `retrieval_mode: "KEYWORD_ONLY"`。

### Chroma 和重排

```powershell
python -m pip install -e "./services/noodle[vector]"
chroma run --path ./data/noodle-next-chroma --host 127.0.0.1 --port 8001
```

在维护器、查询进程或 API 的环境中配置：

```dotenv
NOODLE_NEXT_VECTOR_BACKEND=chroma
NOODLE_CHROMA_HOST=127.0.0.1
NOODLE_CHROMA_PORT=8001
NOODLE_NEXT_RERANK=on
```

重新执行 `next-index` 建立对应后端的代次。默认后端是 `exact`，默认重排关闭；CLI 可用 `--rerank OFF|AUTO|REQUIRED` 单次选择；`AUTO` 在未配置重排器时使用 RRF，运行失败会报告降级；`REQUIRED` 未配置或失败则返回错误。这里实现的是结构化 LLM 重排，还没有专用 cross-encoder。

查询页和后台维护需配置同一向量后端和 profile。Chroma 不可用、覆盖不完整时不会悄悄切换为其他后端。远端 HTTP 的认证、TLS 与企业多租户隔离不在本阶段范围内，入口默认绑定本机。

## API 与 Harness

基础地址为 `http://127.0.0.1:8002`，请求 Schema 见 `/docs`，导出的实际合同见 [运行时 Schema](./schemas/next-v1-p3p4.schema.json)。

| 路径 | 用途 |
| --- | --- |
| `POST /internal/next/search` | 返回正文片段、两类命中数、来源家族数、代次与阶段耗时 |
| `POST /internal/next/answer` | 检索、生成与引用审查 |
| `GET /internal/next/notes/{id}` | 读取当前有效知识页；可约束来源版本 |
| `GET /internal/next/concepts` | 分页搜索 Concept，并显示资格与维护状态 |
| `GET /internal/next/concepts/{id}` | 读取当前 Concept；固定旧版本会被拒绝 |
| `GET /internal/next/concepts/{id}/maintenance` | 贡献决策、处理状态及人工确认所需哈希 |
| `POST /internal/next/maintenance/decisions/{id}/resolve` | 带 `expected_sha256` 接受或跳过待复核贡献 |
| `POST /internal/next/sources` | 登记新 PDF，交给 P2 Worker；不直接完成生成 |
| `GET /internal/next/source-revisions/{id}` | 校验并解析当前来源范围 |
| `GET /internal/next/sources/{id}/file` | 打开当前原 PDF；不是事实检索接口 |
| `GET /internal/next/jobs` | 最近任务与错误 |

冲突解决后，如分析任务仍为 REVIEW，先检查剩余片段，再用 `next-retry --data-dir ... --job-id ... --reason ...` 显式恢复。该命令保留历史检查点，不允许直接把未处理片段标记完成。

Harness 显式设置 `NOODLE_API_BASE_URL=http://127.0.0.1:8002`、`NOODLE_API_PREFIX=/internal/next`。桥接器只注册搜索、回答、读取原子笔记、列出和读取 Concept 这五项工具；旧写工具与旧知识体系工具不注册。限定来源时只检索该 PDF 的原子笔记，不读取跨来源 Concept。指代通过当前实体 ID 解析，不把上次聊天回答当作事实。

## 代码分工

| 模块 | 职责 |
| --- | --- |
| `next/events.py` | P2 / P3 / P4 共用事件扇出 |
| `next/concepts/analysis.py`、`fragments.py` | 完整片段清单、身份与章节匹配、决策审查 |
| `next/concepts/coordinator.py` | 当前贡献快照、构建代次与幂等调度 |
| `next/concepts/builder.py`、`writing.py` | 局部写作、复制未变章节、撤回与条件发布 |
| `next/concepts/review.py` | 人工决策 CAS 与审计 |
| `next/eligibility.py`、`content.py` | SQL 批量筛选及单页读取的有效性屏障 |
| `next/retrieval/` | Chunk、FTS、向量、融合、重排、上下文与引用问答 |
| `next/runtime.py`、`api.py`、`app_cli.py` | 独立维护器、接口和显式命令 |
| `next_0005.py` | 复用 Alembic 的增量迁移，保留旧版本数据 |

## 当前限制

- 归并目录采用完整本地目录，超过 80,000 字符进入复核；单章全部输入超过 36,000 字符也停止，不截断证据。大规模身份候选检索、分层压缩和多目标贡献后续再做。
- FTS 中文是轻量字/二元词，不是领域分词器；分块目标与 0.2 cosine 阈值尚需更多学科基准调参。
- 索引采用完整代次，Embedding 可复用，但旧代次、失活缓存和失败构建产物尚未自动回收。暂不缓存查询结果和回答，避免维护第二套失效规则。
- MOC 只发出 `MOC_ENTRY_SUGGESTED`，不自动修改地图；该事件等待 P5 消费。完整 Vault、双链编辑和新版阅读前端尚未实现。
- 模型审查通过不等于科学准确性已经获得独立人工确认；真实验收与工程测试范围见 [阶段验收记录](./P3-P4-ACCEPTANCE.md) 和 [扩大后的 RAG 实测](./RAG-VALIDATION.md)。

本轮 RAG 扩测进一步统一了拒答/审查失败出口的有效性屏障，并将模型遗漏/重复排序纳入三次有界修正，详见 [RAG 实测](./RAG-VALIDATION.md)。
