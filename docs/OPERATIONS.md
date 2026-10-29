# 使用与运维

更新：2026-10-29。当前业务使用 `next-v1`。开发运行与本机正式库管理是两套运行配置，不能混用停止命令。

## 首次安装

需要 Python 3.12+；Node.js 版本范围见 `package.json`，CI 使用 Node.js 24。

在仓库根目录执行：

```sh
npm ci
python -m pip install -e "./services/noodle[dev]"
npm run build
```

建议在虚拟环境安装。将 `.env.example` 复制为私有 `.env`，只配置实际使用的模型和解析服务。已有 `.env` 不要覆盖；密钥不进入 Git。

启动器优先使用 `NOODLE_PYTHON`，其次仓库 `.venv`，再使用 PATH 中的 Python。选定的环境必须已安装 Python 依赖。

## 通用开发运行

```sh
npm run dev
npm run stop
```

默认工作台 `http://127.0.0.1:8000/app/`，DSH `http://127.0.0.1:3080/`，可以配置调整。`dev:next` 是同一个启动器的兼容命令。

启动器装配 API、Worker 和 Harness；可在启动终端 Ctrl+C，或另开终端运行 `npm run stop`。关闭浏览器不等于关闭后台服务。

只读浏览已有库时，可在启动前设置 `NOODLE_BACKGROUND_WORKERS=0`，避免自动推进已排队任务。

## 当前机器的正式库

在项目根目录的 PowerShell 执行：

```powershell
./scripts/start-production.ps1 -Action Start -Open
./scripts/start-production.ps1 -Action Status
./scripts/start-production.ps1 -Action Restart
./scripts/start-production.ps1 -Action Stop
```

| 操作 | 行为 |
| --- | --- |
| Start | 启动缺少的受管 API、Worker、DSH，沿用正式库 |
| Status | 检查受管进程和服务端口 |
| Restart | 停止受管进程，再加载当前冻结代码启动 |
| Stop | 完整关闭 API、Worker、DSH 及受管监督进程，保留全部数据 |

当前工作台 `http://127.0.0.1:8011/app/`，DSH `http://127.0.0.1:3090/`。

这个脚本仍绑定本机 Conda Python 和 `.local-only/formal-workspace-20261025T043825Z`，不是全新克隆即可使用的通用生产部署器。换机器先用通用入口，并另行配置正式实例；不要复制或公开私有运行目录和密钥。

普通情况下先在前端暂停生成、停止当前对话，再重启服务。未完成任务由租约和检查点恢复；REVIEW 任务不应当成普通排队任务自动整轮重跑。

## 独立命令

```sh
noodle next-api --data-dir ./data/knowledge --host 127.0.0.1 --port 8000
noodle next-run --data-dir ./data/knowledge
noodle next-status --data-dir ./data/knowledge
noodle db-status --data-dir ./data/knowledge
```

API 前缀为 `/internal/next`，旧 `/internal/v1` 不再是当前业务入口。

## 数据位置

普通运行优先使用 `NOODLE_NEXT_DATA_DIR`，其次 `NOODLE_DATA_DIR`，默认 `data/knowledge`。本机正式库为 `data/knowledge-formal-20261025T043825Z`。

| 位置 | 内容 |
| --- | --- |
| `noodle.db` | 来源、版本、证据、任务、贡献、索引元数据与状态 |
| `blobs/`、`uploads/` | 按实际存储规则保存的原始资料和资产 |
| `artifacts/` | 解析、规划、模型回执、构建、问答与验收产物 |
| `Vault/` | 带稳定身份的 Markdown 阅读和本地编辑投影 |
| `.local-only/formal-workspace-*/` | 本机运行配置、进程回执、日志和冻结代码 |

实际子目录以对应库和产物引用为准。不要仅复制数据库并认为已备份正文、图片和向量文件；也不要直接修改不可变产物。

## 导出与恢复

工作台的“数据与同步”入口提供 Markdown、Obsidian 阅读包、Vault 和完整恢复包。

- 阅读包用于脱离应用阅读，不代替完整系统备份。
- 完整恢复同时保留数据库、资产、正文、索引及清单，并验证哈希。
- 恢复或迁移先在独立目录演练，核对库身份、来源和当前版本，再切换服务。
- 来源删除是可恢复的业务操作；恢复资料与重新生成资料不同。
- 冻结代码、历史回执与原件不随代码或文档整理自动删除。

## 常见问题

| 现象 | 先检查 |
| --- | --- |
| 上传后等待 | Worker、消费通道、暂停状态、存储等待；区分排队与执行 |
| REVIEW 或服务欠费 | 查看该阶段真实原因，恢复后从正确检查点继续 |
| 新 PDF 被要求解析缓存 | 是否误用验证模式；正常生产允许新解析 |
| 聊天显示连接恢复 | DSH 就绪、页面脚本、事件连接与会话同步 |
| 任务阶段不变但时间增长 | 区分旧快照与后台实际状态，查看进度读取是否失败 |
| 笔记可读但知识仍整理中 | Atomic 索引与 Concept/结构维护是不同完成边界 |
| 结构部分完成且无待执行项 | 可能涉及历史失败与当前图覆盖，不能只凭标签判断正文损坏 |

排查先读取状态和日志，不盲目重传、重复发送、清空缓存或重跑全库。重启不保证供应商撤回已经收到的请求。
