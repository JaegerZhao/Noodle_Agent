# Noodle knowledge service

Noodle Agent 的 Python 知识领域服务，负责 PDF 解析、笔记生成、证据与版本、检索、可恢复任务及跨文档知识维护。会话与工具编排由项目根目录的 DeepSeek Harness 桥接提供。

从项目根目录安装并启动：

```sh
python -m pip install -e "./services/noodle[dev]"
npm run dev
```

独立进程与数据库命令：

```sh
noodle next-api --data-dir ./data/knowledge --host 127.0.0.1 --port 8000
noodle next-run --data-dir ./data/knowledge
noodle db-status --data-dir ./data/knowledge
noodle db-upgrade --data-dir ./data/knowledge
```

API 提供 `/internal/next`、`/app/` 与 `/health`。当前业务实现集中在 `next/`，外部模型与解析适配器在 `adapters/`，存储迁移在 `storage/`，前端在 `web/next/`。旧业务服务、Synthesis 和旧工作台不再提供执行入口。

API、Worker 和 CLI 使用同一版本迁移入口。迁移及冻结结构随 wheel 打包，工作台模块与渲染资源也在包内。完整项目仍需要 Node/Harness；单独安装 Python 包不会安装会话侧依赖。

详见 [当前入口与清理边界](../../docs/CODEBASE-SIMPLIFICATION-2026-10-26.md)。旧数据库和冻结回执继续保留，不自动迁移为当前知识库。
