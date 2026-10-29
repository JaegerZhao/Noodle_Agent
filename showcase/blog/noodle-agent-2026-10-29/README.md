# 博客发布包

## 文章信息

- 标题：Noodle Agent 实践：从 PDF 笔记到可演化、可溯源的个人知识库
- 建议摘要：记录 Noodle Agent 从 PDF 生成主题笔记、跨资料归并知识、接入 DeepSeek Harness，并实现混合检索、引用溯源和任务恢复的设计与取舍。
- 建议标签：Agent、RAG、个人知识管理、DeepSeek Harness、知识演化
- 八章正文，配七张流程图和七张实际工作台截图。

## 阅读与面试使用

八章正文完整解释需求、内容模型、生成与演化、Agent 接入、检索与引用、可靠性、成本和后续工作，适合作为技术分享文档阅读。

面试时可以沿八章主线讲解，重点展开第三章的三层规划、Atomic 来源约束和 Concept 复用、增量更新与重建，再结合检索、任务恢复和成本取舍回答工程问题。

## 使用方式

`article.md` 是可发布正文。将其与 `assets/` 一同放入博客的文章资源目录，保留相对关系；使用图床时，将 Markdown 图片地址替换成上传后的地址。正文没有本机路径、localhost 地址或依赖项目源码的链接。

流程图提供两倍分辨率 PNG，正文默认使用 PNG；同目录保留原始 SVG，方便按博客主题调整显示或导出。七张工作台截图全部采用用户本轮提供的图片，逐字节复用原文件，没有重绘、裁剪或替换界面内容。

## 新截图位置

| 用户提供的图片 | 正文位置 | 文件 |
| --- | --- | --- |
| 图 1：资料总览与助手 | 第 1 章，工作台能力 | `12-workspace-overview.png` |
| 图 2：知识体系 | 第 3 章，知识组织 | `13-knowledge-systems.png` |
| 图 3：SVM 笔记 | 第 3 章，Atomic 阅读 | `08-atomic-reading.png` |
| 图 4：Transformer 图文 | 第 3 章，Concept 图片贡献 | `14-transformer-content.png` |
| 图 5：任务阶段 | 第 6 章，Worker 与任务状态 | `11-task-stages.png` |
| 图 6：层级可视化 | 第 3 章，关系与阅读入口 | `15-knowledge-graph.png` |
| 图 7：来源笔记 | 第 5 章，跨资料支持与原文定位入口 | `09-concept-sources.png` |

## 素材与内容依据

- 项目介绍：`docs/PROJECT-INTRODUCTION.md`。
- 流程图：`docs/project-introduction-assets-2026-10-29/`。
- 截图原件：本目录 `source-screenshots/`，对应关系与校验值见 `source-screenshots/attachments.json`。
- 本轮核对：`src/read-tools.ts`、`src/next-tools.ts`、`src/tool-contract.ts`、`config/agent-presets/noodle-knowledge/agent.cordis.yml`，以及检索切片、RRF 和模型策略相关代码。
- 规划与正文补充：`next/source_sections.py`、`learning_objects.py`、`learning_labels.py`、`section_contracts.py`、`concepts/entity_routing.py`、`concepts/builder.py` 与 `concepts/entity_patch_v2.py`；以上均位于 `services/noodle/src/noodle_agent/` 下。
- 素材来源、日期、分辨率与 hash：`asset-manifest.json`。

正文旧截图已全部替换，图注直接说明功能，不单独标注截图日期。任务图保留用户提供的实际就绪状态。40.5% Token 降幅仍注明为固定四份 PDF 的历史记录；音视频输入仍为后续方向。本轮未执行模型生成或重新进行问答质量验收。

## 本地重新生成

在本机 Node 环境运行 `node showcase/blog/noodle-agent-2026-10-29/build.mjs`。脚本使用 `sharp`，可以从本地依赖或 Codex 捆绑运行时解析。它复用原图、导出 PNG 并检查 Markdown 图片路径。发布文章本身不需要 Node 环境。
