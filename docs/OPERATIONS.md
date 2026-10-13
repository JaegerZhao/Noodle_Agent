# 运行、配置与数据维护

## 安装与启动

从仓库根目录安装 Node 和 Python 依赖，复制根目录 `.env.example` 为 `.env`。Windows 统一启动使用 PowerShell：

```powershell
npm ci
python -m pip install -e "./services/noodle[dev]"
Copy-Item .env.example .env
npm run dev
```

配置文件已存在时不要重复覆盖。激活 Python 环境，或者通过 `NOODLE_PYTHON` 指定可执行文件。默认入口是工作台 `http://127.0.0.1:8000/app/`、API 文档 `/internal/docs` 和 Harness `http://127.0.0.1:3080`。

`npm run dev` 启动 API、Worker 和 Harness；退出时回收进程，强制关闭终端后可使用 `npm stop`。端口 8000 已被占用时需停止原服务或调整启动参数；Harness 端口由 `NOODLE_DSH_WEB_PORT` 配置，并应同步调整 `NOODLE_HARNESS_WEB_URL`。

Linux/macOS 可以用 Compose，或者在三个终端分别执行：

```sh
python -m noodle_agent.cli serve-internal --host 127.0.0.1 --port 8000
python -m noodle_agent.cli worker
npm run harness
```

独立启动 Harness 改端口时，使用进程环境变量 `DSH_WEB_PORT`；它不应写入 `.env`。Windows 启动器会从 `NOODLE_DSH_WEB_PORT` 转换该值。

## 配置与优先级

Python 配置从当前工作目录读取 `.env`，已经存在的环境变量优先。Node 的 `npm run harness` 也加载根目录 `.env`。建议从仓库根目录运行，并同时核对知识生成和 Harness 会话模型是否可用。

| 配置 | 含义 |
| --- | --- |
| `NOODLE_DATA_DIR` | 知识数据库和文件目录 |
| `NOODLE_DATABASE_URL` | 可覆盖数据库连接；当前实现面向 SQLite，不代表其他数据库已兼容 |
| `NOODLE_EXPORT_DIR` | 导出目录 |
| `NOODLE_PARSER` | `fixture` 或 `mineru` |
| `NOODLE_LLM` | `fixture`、`deepseek`、`openai`、`openai-compatible` |
| `LLM_*` / `DEEPSEEK_*` | 领域生成与问答接口；通用配置优先，特定模型名按账号配置 |
| `EMBEDDING_*` / `DASHSCOPE_*` | 向量服务配置；未提供密钥和模型时使用 HashEmbedding |
| `DOUBAO_API_KEY` / `VISION_*` | 图片理解配置 |
| `KNOWLEDGE_AUTO_SYNC` | 发布和编辑时是否入队增量事件；删除、恢复和已入队处理见 [归并机制](./KNOWLEDGE-SYNC.md) |
| `PARSE_CACHE_ENABLED` | 是否复用相同内容的规范化解析 |
| `*_CONCURRENCY` | 规划、生成、Judge、视觉处理的有界并发 |
| `WORKER_*` | 资料任务租约、心跳与轮询；同步事件租约当前在代码中固定为 180 秒 |

根目录模板使用 `./data/noodle`。直接调用 Python 且没有配置时，数据默认是 `./.noodle`。**Windows 统一启动器在未设置进程环境变量时会显式指定仓库的 `data/noodle`，仅更改 `.env` 的 `NOODLE_DATA_DIR` 不会覆盖这个值。** 自定义目录可以在启动前设置：

```powershell
$env:NOODLE_DATA_DIR = "D:/NoodleData"
$env:NOODLE_EXPORT_DIR = "D:/NoodleData/exports"
npm run dev
```

相对路径按工作目录解析。API 和 Worker 应共享同一知识库；不要让两个无协调的独立部署同时使用它。向量服务变更后须重新准备相应索引，同一模型名下的维度或模型行为变化也需要注意旧向量兼容性。

## 无密钥验证与真实资料

```sh
python scripts/delivery-check.py --browser
```

该脚本在隔离目录中创建合成 PDF、安装 fixture sidecar、启动真实进程并验证阅读器，报告给出产物位置。没有浏览器时省略 `--browser`；浏览器路径可通过 `EDGE_EXECUTABLE` 设置。

`python -m noodle_agent.cli create-demo --dir .runtime/demo-input` 只创建合成输入。它与隔离脚本是独立入口，生成的 sidecar 不会随着普通工作台 PDF 上传自动复制。fixture 不能理解任意 PDF；真实资料需要配置 MinerU、生成模型及可用密钥。真实模式会将文件、片段或图片发送到相应服务。

## Docker Compose

Compose 把宿主机 `data/noodle/` 挂载为 `/data/noodle`，把 `data/dsh/` 挂载为 Harness 的 `/data/dsh`。当前固定本机映射端口为 8000 和 3080，脚本专用的端口变量不会自动改变 Compose 映射。

根目录 `.env.example` 的导出路径是相对路径。**使用当前 Compose 时，应在容器使用的 `.env` 中设置 `NOODLE_EXPORT_DIR=/data/noodle/exports`，使导出位于持久化挂载内。** Windows 本地模式与容器模式应使用各自合适的导出路径。

```sh
docker compose config --quiet
docker compose up --build
```

CI 验证镜像构建和 Compose 配置，未覆盖真实模型下的完整容器 UI 演示。

## 存储与备份

| 位置 | 内容 |
| --- | --- |
| `data/noodle/noodle.db` | 笔记正文、知识点、体系、证据、向量、任务和历史版本 |
| `data/noodle/blobs/` | 按内容哈希保存的 PDF 和图片 |
| `data/noodle/uploads/` | 上传接收文件 |
| `data/noodle/artifacts/` | 解析和生成阶段产物 |
| `data/noodle/exports/` | 按本地模板配置的 Markdown 导出 |
| `data/noodle/logs/` | 领域日志 |
| `.runtime/dsh-home/` | 默认本地 Harness 配置与会话，含 `sessions/`、`storages/` |
| `.runtime/local/` | 启动日志与 PID |

`DSH_HOME` 或 `NOODLE_RUNTIME_DIR` 可以改变 Harness 状态位置。备份前停止 API、Worker 和 Harness，保存实际知识数据目录、原文与产物、导出目录、Harness home 和私有配置。SQLite 与 Blob 需要一起保存；`.runtime/` 包含会话，不能全部当作临时缓存。

来源删除是逻辑删除，原文件、历史、导出和备份仍可能存在。当前没有覆盖全部存储的统一永久清除 API。

移动已有安装目录时，需处理产物和数据库中的绝对路径、Harness 工作区映射与会话目录，并重新安装 Python editable 包。当前没有通用的一键搬迁命令。

## 数据库升级现状

服务启动使用 `Database.init()` 创建缺失表并补充部分字段、索引。仓库另有 Alembic `0001` 至 `0009` 迁移与测试，常规启动不自动运行 `alembic upgrade head`。

两条路径尚未统一。自动补齐不会同步推进 Alembic 版本号；初始迁移又引用当前 ORM 模型，不能把记录的迁移号当作完整结构证明。更新有真实数据的安装前，应备份并检查版本变化，避免直接给未经核对的数据库设置迁移号。当前没有保证任意旧库均能无损升级或降级的承诺。

## 故障排查与部署边界

- 没有笔记：查看 Job 时间线、Worker 日志和解析器配置。
- 笔记完成但知识点未更新：查看同步事件及 proposals；待复核与资料 Job 是不同状态。
- 问答无命中：检查资料范围、当前有效来源与向量模型配置。
- 页面异常：查看浏览器/API 日志，静态资源更新后刷新缓存。
- 模型超时或限流：降低相应并发并查看阶段产物，决定是否重试。

内部 API 面向可信本地环境，缺少完整认证和多用户隔离。远程部署需要补充认证、HTTPS、访问控制和限流。默认不要将这些内部接口直接开放到公网。
