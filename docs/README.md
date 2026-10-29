# Noodle Agent 文档导航

整理日期：2026-10-29。此目录只维护当前项目说明和一份开发日志；历史方案、逐轮验收与交接材料已归档，不再作为当前运行指引。

| 文档 | 用途 |
| --- | --- |
| [项目介绍](PROJECT-INTRODUCTION.md) | 背景、技术主线、真实案例，适合面试和技术分享 |
| [产品需求与实现边界](PRODUCT-REQUIREMENTS-AND-IMPLEMENTATION.md) | 核心对象、用户流程、当前能力和未完成项 |
| [系统架构](ARCHITECTURE.md) | 当前代码入口、生成与维护、版本和证据约束 |
| [Agent 与 RAG 详解](AGENT-AND-RAG.md) | DSH、17 个领域工具、Agent Loop、记忆、检索与审批 |
| [使用与运维](OPERATIONS.md) | 安装、启动、完整关闭、数据位置、恢复和故障处理 |
| [验证与已知问题](TESTING.md) | 检查命令、最近结果、质量边界和后续优先级 |
| [开发更新日志](DEVELOPMENT-LOG.md) | 按原始材料日期汇总更新、失败、恢复和验收，附原文日期索引 |

建议阅读顺序：项目介绍 → 产品需求 → 系统架构 → Agent/RAG；实际使用先看运维说明，查看历史则读更新日志。

`schemas/`、`examples/`、`design/examples/` 和 `evaluations/` 是契约、示例和评测数据，继续保留。`project-introduction-assets-2026-10-29/` 保存项目图示，`assets/` 保存日志引用的截图。

博客与演示幻灯片移至 [showcase](../showcase/README.md)，资源随目录保留。

整理前的 237 个文件已完整压缩归档，并逐文件核对 SHA-256。私有归档位于 `.local-only/docs-consolidation-2026-10-29/`，含原始 ZIP、哈希清单和迁移映射，不进入 Git。恢复历史原文时应查归档，不要以修改时间反推实施日期。

当前状态以源码、实际回执和注明日期的验证结果为准。日志中的通过、数量、模型和耗时只属于对应实验，不代表当前版本在所有输入上均通过。
