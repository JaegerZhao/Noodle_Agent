# Noodle Agent

将 PDF 整理为有来源、可修订的笔记，再汇总为跨资料的知识体系。当前工作台、API、worker 与 DSH 桥接统一使用 `next-v1`。

右侧助手统一使用 DeepSeek Harness 会话，调用 17 个领域工具完成检索问答、来源/任务发现与受审批操作。当前验证边界见 [验证说明](docs/TESTING.md)，逐日记录见 [开发日志](docs/DEVELOPMENT-LOG.md)。

框架装配、Agent Loop、记忆、任务编排、混合检索和对话能力详见 [DSH Agent 与 RAG 架构详解](docs/AGENT-AND-RAG.md)。

## 当前流程

PDF → MinerU 解析 → 资料类型与目录规划 → 综合 Atomic 笔记 → 向量索引与浏览。

Atomic 发布后，Concept 归并与知识组织在后台继续。书籍按原目录划分章节批次；课件和论文使用同一流水线的资料配置。教材是 FOUNDATION 基础来源，课件与论文可作为 SUPPLEMENT 补充来源。

前端支持资料阅读、笔记与原始 PDF 对照、完整书籍目录、知识点、多个根节点的知识树和关系图。Markdown/Vault 是带稳定身份的阅读与编辑投影，数据库版本及证据关系保持权威。

## 安装与启动

需要 Python 3.12+、Node.js 24 或 package.json 支持的版本。

```sh
npm ci
python -m pip install -e "./services/noodle[dev]"
npm run build
```

复制 `.env.example` 为私有 `.env`，配置实际使用的 MinerU、DeepSeek、视觉与嵌入服务。密钥和知识数据不进入 Git。

```sh
npm run dev
npm run stop
```

`npm run dev:next` 是同一启动器的兼容命令，不再启动第二套工作台。默认地址为 `http://127.0.0.1:8000/app/`；Harness 为 `http://127.0.0.1:3080/`，均可配置。

普通本地进程使用 `NOODLE_NEXT_DATA_DIR`，或 `NOODLE_DATA_DIR`，默认 `data/knowledge`。只浏览现有内容时，可设置 `NOODLE_BACKGROUND_WORKERS=0` 后启动；这样不会自动推进队列中的生成任务。

## 独立命令

```sh
noodle next-api --data-dir ./data/knowledge --host 127.0.0.1 --port 8000
noodle next-run --data-dir ./data/knowledge
noodle next-status --data-dir ./data/knowledge
noodle db-status --data-dir ./data/knowledge
noodle next-outline --data-dir ./data/knowledge --source-id <source-id>
```

API 路径为 `/internal/next`。旧 `/internal/v1`、Synthesis 业务和旧工作台已退出当前执行路径。历史数据库、原件、模型回执及冻结代码不会自动删除或迁移；旧冻结运行仍使用自己的代码快照。

DSH 只注册当前检索、问答、笔记读取、知识导航和受审批保护的章节编辑工具。生成、索引、版本保护与失效判断仍由 Python 应用统一执行。

## 开发验证

当前机器的正式库服务使用以下命令管理（在项目根目录的 PowerShell 中执行）：

```powershell
./scripts/start-production.ps1 -Action Start -Open
./scripts/start-production.ps1 -Action Status
./scripts/start-production.ps1 -Action Restart
./scripts/start-production.ps1 -Action Stop
```

`Stop` 关闭正式 API、Worker、DSH 及受管监督进程，保留知识、任务检查点和对话历史。关闭浏览器或启动终端不会关闭这些后台服务。`npm stop` 对应开发运行，不作为本机正式库的关闭入口。

```sh
npm run typecheck
npm test
npm run build
npm run check:release
npm run test:python
npm run test:launcher
npm run smoke:workflow
```

离线测试显式注入提供者。启动检查使用无密钥的临时知识库和随机端口，不读取正式资料。真实模型验证与内容验收需要单独安排，不能用离线通过代替质量结论。

## 结构与文档

- `services/noodle/src/noodle_agent/next/`：版本化知识、生成、归并、组织、检索、API 与调度。
- `services/noodle/src/noodle_agent/adapters/`：MinerU、结构化模型、视觉与嵌入适配器。
- `services/noodle/src/noodle_agent/storage/`：数据库连接与版本迁移；冻结迁移资源保留。
- `services/noodle/src/noodle_agent/web/next/`：当前工作台与 Markdown 渲染。
- `src/`：DSH 当前工具、HTTP 能力桥接及工作区注册。
- `scripts/`：启动、构建、恢复、审计与可复用验证入口。

[文档导航](docs/README.md) · [当前架构](docs/ARCHITECTURE.md) · [产品需求](docs/PRODUCT-REQUIREMENTS-AND-IMPLEMENTATION.md) · [使用与运维](docs/OPERATIONS.md)

[DSH 与 RAG 架构](docs/AGENT-AND-RAG.md) · [开发日志与验收记录](docs/DEVELOPMENT-LOG.md)

历史设计和逐轮验收已汇总到开发日志，原文留在私有文档归档中；旧命令不代表当前执行入口。私有数据、隔离备份和冻结运行记录位于被 Git 忽略的 data、.local-only、.runtime 等目录。
