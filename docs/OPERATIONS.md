# 运行、配置与备份

## 运行模式

Windows 的 `npm run dev` 启动精简 API、Worker 和 Harness，服务信息与日志记录在 `.runtime/local/`。使用激活环境中的 Python，或者设置 `NOODLE_PYTHON`。`npm stop` 只清理项目 PID 文件记录的进程。

在其他平台，可以用 Docker Compose，或者在三个终端分别启动：

```sh
# 从仓库根目录运行，先安装 Python 与 Node 依赖
python -m noodle_agent.cli serve-internal --host 127.0.0.1 --port 8000
python -m noodle_agent.cli worker
npm run harness
```

先复制根目录 `.env.example` 为 `.env`。独立启动 Harness 时，需要设置 `DSH_WEB_PORT` 环境变量才能改变其端口；Windows 统一启动器会把 `NOODLE_DSH_WEB_PORT` 转换为该变量。使用统一工作台时，`NOODLE_HARNESS_WEB_URL` 应指向实际 Harness 地址。

## 主要配置

| 配置 | 含义 |
| --- | --- |
| `NOODLE_DATA_DIR` | SQLite、Blob、解析与生成产物目录 |
| `NOODLE_EXPORT_DIR` | Markdown 等导出目录 |
| `NOODLE_PARSER` | `fixture` 或 `mineru` |
| `NOODLE_LLM` | `fixture`、`deepseek`、`openai` 或 `openai-compatible` |
| `LLM_*` / `DEEPSEEK_*` | 知识生成与问答配置；通用配置优先 |
| `DASHSCOPE_*` / `EMBEDDING_*` | 向量接口、模型和维度 |
| `DOUBAO_API_KEY` / `VISION_*` | 图片理解接口 |
| `KNOWLEDGE_AUTO_SYNC` | 是否自动维护跨文档知识 |
| `PARSE_CACHE_ENABLED` | 是否复用相同内容的规范化解析 |
| `*_CONCURRENCY` | 规划、正文、Judge 与视觉任务的有界并发 |
| `WORKER_*` | 轮询、租约与心跳间隔 |

不同 `.env` 的数据目录不得被两个独立部署同时写入。相对路径按启动工作目录解析，建议始终在仓库根目录启动。更改向量模型或维度后应重新建立对应索引，不能混用不同模型产生的向量。

## 无密钥示例

```sh
python -m noodle_agent.cli create-demo --dir .runtime/demo-input
python scripts/delivery-check.py --browser
```

第一个命令创建项目自产的合成 PDF 和解析 sidecar。第二个命令执行隔离演示链路，并在报告中给出产物目录。fixture 需要合成 sidecar；普通工作台上传不会自动把另一个目录的 sidecar 视为解析结果。任意真实 PDF 应切换到 MinerU。

## 备份与迁移

先停止 API、Worker 和 Harness，再备份完整 `data/noodle/`、本地 `.env` 和 Harness 状态目录。SQLite 与文件产物共同构成知识库，只备份数据库不能恢复原 PDF 和图片。

资料删除属于逻辑删除，会停止其有效引用，不等同于物理擦除 Blob 或永久清除历史。处理个人数据时需要同时考虑原文件、历史版本、导出与备份。当前没有统一永久清除 API。

移动已经有真实数据的安装目录时，需处理持久化产物中的绝对路径，并重新安装 Python editable 包；仅改目录名称可能使旧图片或解析产物无法读取。新克隆与新数据目录不需要迁移。

## 故障排查

- 工作台显示异常：查看 API 与浏览器日志；更新静态资源后刷新浏览器缓存。
- 上传完成但没有笔记：查看 Job 的阶段、时间线和错误，确认 Worker 在运行，且解析器不是缺少 sidecar 的 fixture。
- 笔记完成但知识点未更新：检查自动同步开关、知识同步事件与候选状态；待复核不表示生成任务失败。
- 问答没有命中：确认资料笔记已发布、当前资料范围是否正确、有效来源是否仍存在，以及向量配置是否一致。
- 模型超时或限流：检查服务商配置，按额度降低并发；任务重试前查看保留的阶段产物。

## 部署边界

服务默认用于本机可信环境。内部 API 可以读取、上传和修改知识，不能直接暴露到公网。远程使用需要补充认证、访问控制、HTTPS、限流和隔离存储。当前 CI 检查部署配置与构建，不代表生产安全或多租户能力已验收。
