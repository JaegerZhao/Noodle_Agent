# Noodle knowledge service

这是 Noodle_Agent 内部的 Python 知识领域服务，保留 PDF 解析、规划、笔记生成、
质量校验、版本、证据、图片、索引和可恢复 Job。

统一项目使用精简入口：

```powershell
noodle serve-internal --host 127.0.0.1 --port 8000
```

该入口只暴露 `/internal/v1`，不会初始化旧 LangGraph Agent 或旧 Web UI。
后台流水线由独立进程运行：

```powershell
noodle worker
```

旧 `noodle serve` 仅作为迁移期回滚入口保留，不是 Noodle_Agent 的默认运行方式。
