# 运行、配置与数据维护

## 安装与启动

需要 Node.js 22.19+ 或 24+，以及 Python 3.12+。从仓库根目录执行：

```sh
npm ci
python -m venv .venv
# Windows PowerShell：.\.venv\Scripts\Activate.ps1
# Linux/macOS：source .venv/bin/activate
python -m pip install -e "./services/noodle[dev]"
# Windows：Copy-Item .env.example .env
# Linux/macOS：cp .env.example .env
npm run dev
```

已有 `.env` 时不要覆盖。启动器优先使用 `NOODLE_PYTHON` 指定的 Python，其次仓库 `.venv`，最后 PATH 中可用的 Python。确保选中的环境已安装依赖。

`npm run dev` 在三个平台使用同一 Node 入口：先构建 TypeScript，检查端口，启动 API 并等待健康检查，再启动 Worker 和 Harness。子服务意外退出时清理其余进程并返回失败。Ctrl+C 退出；终端被强制关闭后可用 `npm stop` 清理。停止脚本同时核对 PID 和本项目脚本路径，避免结束 PID 已复用的其他进程。

默认工作台为 `http://127.0.0.1:8000/app/`，API 文档为 `/internal/docs`，Harness 为 `http://127.0.0.1:3080`。可在 `.env` 设置 `NOODLE_API_PORT`、`NOODLE_DSH_WEB_PORT`。统一启动器据此生成服务地址；单独启动或容器模式需自行保证地址与监听端口一致。

也可在独立终端执行 `npm run api`、`npm run worker`、`npm run harness`。`npm run setup:python` 安装 Python 开发依赖。

## 配置与优先级

Node 入口加载根目录 `.env`，已存在的进程环境变量优先；Python 直接启动时从工作目录读取 `.env`。统一启动器补充默认路径，但保留 `.env` 中配置的数据和导出目录。

| 配置 | 含义 |
| --- | --- |
| `NOODLE_PYTHON` | 本地 Python 可执行文件 |
| `NOODLE_DATA_DIR` / `NOODLE_EXPORT_DIR` | 知识数据与 Markdown 导出目录 |
| `NOODLE_DATABASE_URL` | 数据库 URL；当前迁移只支持 SQLite |
| `NOODLE_RUNTIME_DIR` / `DSH_HOME` | 启动状态、日志及 Harness 配置与会话位置 |
| `NOODLE_PARSER` | `fixture` 或 `mineru` |
| `NOODLE_LLM` | `fixture`、`deepseek`、`openai`、`openai-compatible` |
| `LLM_*` / `DEEPSEEK_*` | 领域生成与问答接口；通用配置优先 |
| `EMBEDDING_*` / `DASHSCOPE_*` | 向量接口；未配置时使用 HashEmbedding |
| `DOUBAO_API_KEY` / `VISION_*` | 图片理解接口 |
| `KNOWLEDGE_AUTO_SYNC` | 发布与编辑的增量事件开关；删除、恢复及已入队任务见 [归并机制](./KNOWLEDGE-SYNC.md) |
| `PARSE_CACHE_ENABLED` / `*_CONCURRENCY` | 解析缓存与各阶段有界并发 |
| `WORKER_*` | Job 租约、心跳和轮询；知识同步事件租约目前固定 180 秒 |

统一 npm 入口默认使用仓库 `data/noodle/`。无配置而直接调用 Python 时默认是工作目录 `.noodle/`。相对数据路径按启动工作目录解析，API 和 Worker 必须指向同一库。自定义位置可直接写入 `.env`：

```dotenv
NOODLE_DATA_DIR=D:/NoodleData
NOODLE_EXPORT_DIR=D:/NoodleData/exports
```

Windows 建议将 Harness home 与仓库放在同一磁盘；依赖链接跨盘可能受 Harness 底层路径限制影响。更换向量服务、维度或模型行为时，需要处理已有索引兼容性。

## 无密钥验证与真实资料

```sh
npm run test:launcher
python scripts/delivery-check.py --browser
```

脚本使用隔离目录和合成资料，不读取本地 `.env` 或实际知识库。浏览器检查需要已安装的 Chromium，可设置 `EDGE_EXECUTABLE`；没有浏览器时省略 `--browser`。

fixture 消费合成解析 sidecar，不能理解任意上传 PDF。`noodle create-demo --dir .runtime/demo-input` 生成合成输入，但普通上传不会自动传递 sidecar。真实资料需要配置 MinerU、生成模型和向量服务，Harness 会话还需要独立可用的模型。真实模式会向配置服务商发送文件、相关片段或图片。

## Docker Compose

```sh
docker compose config --quiet
docker compose up --build
```

Compose 启动 API、Worker、Harness，默认只映射本机 8000 与 3080。知识目录挂载到 `/data/noodle`，导出目录显式配置为 `/data/noodle/exports`，Harness home 挂载到 `/data/dsh`。修改本地启动端口变量不会自动修改 Compose 端口映射。

项目通过外部模型接口运行，不要求 Docker Model Runner。Docker Desktop 自身启动失败时先修复引擎；不要为排查通信文件错误直接恢复出厂设置。

## 数据与备份

| 位置 | 内容 |
| --- | --- |
| `data/noodle/noodle.db` | 正文、证据、向量、任务、知识点与版本 |
| `data/noodle/blobs/` | 按哈希保存的 PDF 和图片 |
| `data/noodle/uploads/` | 上传接收文件 |
| `data/noodle/artifacts/` | 解析与生成产物 |
| `data/noodle/exports/` | Markdown 导出 |
| `data/noodle/backups/` | 结构升级前的 SQLite 备份 |
| `.runtime/dsh-home/` | 默认 Harness 配置、会话与存储 |
| `.runtime/local/` | 启动日志与 PID 记录 |

路径可由配置改变。完整备份前停止服务，保存实际知识目录、Blob、产物、导出、Harness home 和私有配置。数据库自动备份只保护结构升级前的数据库，不代替完整业务备份。`.runtime/` 可能包含会话，不能整体当作缓存删除。

来源删除为逻辑删除，历史、原文件、导出和备份仍可能保留；没有统一永久清除 API。移动已有安装需要处理数据库与产物中的绝对路径、Harness 工作区映射以及 editable 安装，没有通用一键搬迁命令。

## 唯一数据库升级机制

```sh
npm run db:status
npm run db:upgrade
```

`db:status` 只查询迁移号。API、Worker 启动时和 `db:upgrade` 共用 `storage/runner.py`，不再另行通过 ORM `create_all` 或启动补丁升级结构。当前迁移头为 `0010`。

迁移保留 `0001` 至 `0009` 的版本身份，使用冻结 DDL 和显式历史变更；`0010` 接管已知的旧库补列、索引和废弃表兼容。无迁移号的已知旧库也沿同一路径检查与升级。旧 Agent 表组仅在全部为空时清理；有历史数据时保留物理表，当前服务不加载旧 Agent ORM。

升级在 SQLite `BEGIN IMMEDIATE` 事务中执行，跨进程写锁和进程内 Alembic 锁协调冷启动。需要结构升级且已有业务表时，先通过 SQLite backup API 创建一致的数据库副本。异常回滚并保留备份；不支持的旧结构不会直接 stamp 到最新版本。模型与冻结结构的一致性由回归测试检查。

当前不承诺任意旧结构兼容，也不支持自动降级。需要撤销升级时，停止全部服务，用升级前数据库备份恢复，并切回匹配的旧代码版本；回滚前另存当前数据，避免覆盖升级后的业务变更。

## 排查与部署边界

- 无笔记：查看 Worker 日志、解析配置和 Job 时间线。
- 归并未更新：查看独立同步事件、候选和待复核原因。
- 检索无命中：检查资料范围、有效来源和向量模型。
- 页面异常：查看浏览器与 API 日志；更新后刷新资源缓存。
- 升级失败：保留日志和备份，停止服务核对库结构，不要手工改迁移号。

内部 API 面向可信本地环境，缺少完整认证和多用户隔离。远程部署需另行补充认证、HTTPS、权限控制和限流。
