# 新系统实施与阶段验收记录

更新：2026-10-21；方案：`next-v1`。本批实现保存在本地仓库，尚未推送。

## 当前进度

已交付第一批工程实现：原件清单与恢复基线、独立 next-v1 数据库、统一内容发布和持久任务底座。默认 API、Worker、工作台与 DSH 仍使用原系统；没有把空的新库切换为用户当前知识库。

| 阶段 | 实际状态 | 尚需完成 |
| --- | --- | --- |
| P0 | 原件登记、工程基线、快照与实际恢复已通过 | 问答种子已建立；真实生成/检索对照、完整内容金标及独立人工评测尚未完成 |
| P1 | 数据契约、共享 Alembic、发布与任务基础设施已实现并通过工程测试 | 接入后续生成/索引/归并消费者；最终切换前重新验收 |
| P2 | 未实施 | 原 PDF 冷解析、新规划、统一文风生成与真实阅读验收 |
| P3 | 未实施 | Concept 身份/章节/增量分析、自动局部重建和质量门禁 |
| P4 | 未实施 | Chunk、向量后端对照、融合/重排、真实 RAG 与快照 |
| P5 | 未实施 | 完整新前端、MOC 自动编排、Vault 回流与导出 |
| P6 | 未实施 | 全量原 PDF 重建与切换 |
| P7 | 未实施 | 结束恢复窗口，清理旧代码、旧生成数据并发布 |

工程底座先独立交付，内容基线与真实链路继续建立。不能将本批通过数解释为全部阶段、28 个场景或真实内容质量已经达标。

## 原件与恢复

- 快照覆盖旧知识数据库及 blobs、artifacts、exports、uploads；未复制历史备份目录与日志。Harness 会话和当前本地配置未迁移，新系统尚未接管它们。
- 4,521 个文件，共 746,299,390 字节，逐文件 SHA-256 校验通过；SQLite integrity_check 通过，外键违规为零。
- 已在独立目录实际恢复全部清单文件，重新核对文件哈希和数据库记录数，结果一致。
- 旧库 23 个来源版本：22 个 PDF 版本，对应 8 个不同文件哈希；PDF 原件哈希全部匹配。
- 另一个旧教材 JSON 清单的来源哈希表示教材仓库内容，与清单文件哈希不同；其物理 Blob 哈希匹配。该非 PDF 导入按方案排除，不转换旧教材笔记。
- 两份用户指定课件可读，分别为 59 页、152 页。已从原 PDF 建立 7 个含页码/引用片段的问答种子；它们不是已通过的 RAG 问答结果。

本地记录位于 `.local-only/rebuild-2026-10-21/`：baseline/manifest.json、restore-check/、source-inventory.json、evaluation-seed.json、python-p0p1.xml、wheel-check.json。含私人原件和绝对路径，不进入 Git。

## 已实现的边界

| 能力 | 实际实现与验证 |
| --- | --- |
| 存储隔离 | `next-v1` 契约标记；误指旧库时拒绝结构变更，旧运行入口也拒绝新库 |
| 数据库升级 | 复用唯一 `storage.runner.upgrade` 与 Alembic 环境；按契约选择迁移位置，不另建升级器 |
| 新基线 | `next_0001` 创建新模型，`next_0002` 增加事件/任务幂等索引，`next_0003` 增加全局活动政策；增量升级保留数据并生成备份 |
| 来源与证据 | 文件去重、来源版本替换、显式 AUTHOR 来源；PDF 证据必须匹配指定解析结果、页码和块 |
| 内容发布 | ATOMIC/CONCEPT/MOC 共用候选、章节、Markdown 快照与发布原语；检查报告绑定候选哈希与政策 |
| 来源资格 | ATOMIC 绑定一个来源版本；Concept 输入只能为当前有效 ATOMIC，选择片段与证据必须对应 |
| 立即失效 | 来源删除/替换、原子笔记新版本或撤回后，相关旧知识停止正常读取；重建消费者尚未实现 |
| 事务与并发 | 先写不可变正文，再条件提交活动指针、输入、事件和构建记录；陈旧版本拒绝；outbox 异常回滚 |
| 人工保护 | 受保护正文不能被自动改写、删除或取消保护；显式作者编辑接口仍待后续接入 |
| MOC 底层 | 引用同一知识身份，检查循环/错误固定版本，编译相对 Markdown 链接；完整地图 UI 尚未实现 |
| 语料约束 | 数据库拒绝为 MOC 建立知识 Chunk，并限制片段在所属知识章节范围；实际索引后端尚未实现 |
| 任务与租约 | outbox 扇出、幂等任务、并发领取、独立租约令牌、心跳、过期接管、有界错误重试与崩溃接管，以及事件完成检查 |
| 政策一致性 | 数据库持有活动写作政策，政策变化推进有效性代次；旧政策进程不能继续读取或发布 |
| 发布 fencing | 后台发布检查当前任务租约，过期 Worker 不能提交；不能只检查 Worker 名称 |
| 可交付性 | 实际构建 wheel，从解包安装内容初始化 next_0003，并确认迁移 JSON 和 Python 模块齐全 |

检查报告用于承载应用侧复核；本批测试使用明确的合成 fixture 审查。真实 LLM Judge、来源覆盖和科学准确性尚未验证，不因某个对象带 `passed=true` 就宣称内容合格。

实现按显式依赖拆分为 [来源与证据](../services/noodle/src/noodle_agent/next/sources.py)、[内容读取](../services/noodle/src/noodle_agent/next/content.py)、[发布](../services/noodle/src/noodle_agent/next/publication.py)、[存储事务](../services/noodle/src/noodle_agent/next/repository.py)、[任务](../services/noodle/src/noodle_agent/next/jobs.py)；[Store](../services/noodle/src/noodle_agent/next/store.py) 仅负责装配。没有给新业务再引入共享 owner 门面。

## 执行结果

| 检查 | 结果 |
| --- | --- |
| Python 全量回归 | 146 通过，0 失败/错误/跳过；其中新增 36 个测试 |
| TypeScript 测试 | 9 通过 |
| TypeScript 类型与构建 | 通过 |
| Python 全仓静态检查及备份脚本检查 | 通过 |
| 发布源文件检查 | 通过；不等于完整历史安全审计 |
| Windows 隔离启动器 | 通过，API/Worker/Harness 启动与停止正常 |
| 隔离交付检查 | 通过，fixture 上传、独立 Worker、检索引用回答与真实 Harness 工具运行时；未执行真实 LLM 回合 |
| next-v1 JSON Schema 严格编译 | 通过 |
| wheel 资源与实际初始化 | 通过 |
| 旧库来源/版本/笔记/Concept/任务计数 | 与快照一致 |
| 新前端、真实生成、真实问答、Obsidian | 尚未执行，不记为通过 |

首次 Python 基线曾因系统临时目录权限产生 fixture 错误；改用仓库内独立临时目录后，原有回归全部通过。未通过修改业务断言掩盖错误。启动/交付命令首次因当前终端未激活 Python 环境退出，显式设置 `NOODLE_PYTHON` 后通过，未修改用户本地配置。

测试主要覆盖 T01/T04/T12/T23 的底座断言，以及来源失效、人工保护、MOC、语料边界等前置断言；T09 等完整场景还需要自动重建与真实问答，当前不标记整项完成。

## 现在可使用的命令

在项目根目录、已安装项目依赖的 Python 环境执行：

```sh
python -m noodle_agent.cli next-init --data-dir ./data/noodle-next
python -m noodle_agent.cli next-schema
```

第一条实际初始化或升级隔离新库，不读取 `.env` 的旧知识库配置；当前新库无旧笔记、旧 Concept 或旧索引。它不是启动新前端的命令。第二条输出真实运行时 Candidate 与 Review 的 Schema；导出的 [组合 JSON Schema](./schemas/next-v1-content.schema.json) 用于结构校验，不能代替来源、语义和发布事务。

恢复工具 [rebuild-baseline.py](../scripts/rebuild-baseline.py) 提供 create/verify/restore。create 和 restore 要求新的输出目录，拒绝与原目录相互包含；不会覆盖当前业务库。

## 下一批实施

P2 将现有 MinerU/视觉适配器接到新数据模型：持久化解析与证据 → 跨窗口去重和覆盖规划 → 统一 Markdown 写作 → 绑定真实审查 → 发布。需要为任务恢复增加笔记创建与阶段产物幂等记录，避免崩溃重跑产生重复活动笔记；之后使用神经网络课件做首次完整冷解析和阅读验证。

只有真实生成与后续链路通过后，才推进全库重建、默认运行切换及旧数据清理。本批没有上传 PDF 到外部服务、删除旧笔记、改动旧前端或推送 GitHub。
