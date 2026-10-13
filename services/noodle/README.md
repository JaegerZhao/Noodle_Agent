# Noodle knowledge service

Noodle Agent 的 Python 知识领域服务，负责 PDF 解析、笔记生成、证据与版本、检索、可恢复任务及跨文档知识维护。会话与工具编排由项目根目录的 DeepSeek Harness 桥接提供。

从项目根目录安装并启动：

```sh
python -m pip install -e "./services/noodle[dev]"
npm run dev
```

独立进程与数据库命令：

```sh
noodle serve-internal --host 127.0.0.1 --port 8000
noodle worker
noodle db-status
noodle db-upgrade
```

API 提供 `/internal/v1`、`/app/` 与 `/health`；交互文档为 `/internal/docs`。源码按 `application/`、`http/`、`storage/`、`adapters/knowledge/` 和 `synthesis/semantic/` 组织。历史 Agent/API/UI 已移除。

API、Worker 和 CLI 使用同一版本迁移入口。迁移及冻结结构随 wheel 打包，工作台模块与渲染资源也在包内。完整项目仍需要 Node/Harness；单独安装 Python 包不会安装会话侧依赖。

详见 [架构](../../docs/ARCHITECTURE.md)、[运行与备份](../../docs/OPERATIONS.md) 和 [验证](../../docs/TESTING.md)。
