# 初始开源快照验证记录

本记录固定对应 2026-10-13 的初始开源提交 [`26a6fd6`](https://github.com/JaegerZhao/Noodle_Agent/commit/26a6fd65320376476e33747f11331243116d646a)。它是历史验证记录，后续版本的状态应查看相应提交的 CI，不能沿用这里的测试数量或结论。

## 验证结果

| 检查 | 结果与范围 |
| --- | --- |
| TypeScript 类型检查、构建和契约测试 | 本地通过，9 项测试 |
| Python 全量 Ruff 与回归 | 本地通过，105 项测试 |
| 独立公开源码克隆 | npm 依赖安装、构建、回归及隔离演示通过；复用本机 Python 依赖环境 |
| 合成 PDF → API → Worker → 笔记 | 通过 |
| 证据检索与 fixture 引用回答 | 通过，未评估真实模型回答 |
| Harness 工具与同源会话代理 | 通过，未运行真实 LLM 会话轮次 |
| 浏览器阅读器、PDF.js、Markdown、KaTeX | 本地通过，无页面脚本异常 |
| Python wheel 与前端资源 | 本地构建及资源检查通过 |
| Compose 配置 | 本地与 Linux CI 通过 |
| Linux Docker 镜像 | CI 构建通过，本地未运行 Docker 引擎 |
| Windows/Linux CI | 两个作业通过 |
| 公开文件与文档链接 | 当次检查通过，源码敏感检查采用启发式规则 |

CI 结果：[初始提交验证运行](https://github.com/JaegerZhao/Noodle_Agent/actions/runs/38342619155)。镜像构建通过不代表完整容器交互或生产部署已经验收。

## 内容与发布范围

隔离演示使用项目自产 PDF 和 fixture，不读取本地密钥、课程资料或知识库。`assets/workbench-demo.png` 来自该合成演示，用于展示阅读器布局，不是实际课程内容或真实聊天质量样例。

公开仓库使用新的提交历史，不含原私有历史、真实课程、生成知识数据、模型配置和个人演示档案。工作台、知识版本与同步功能属于早期开源版本；内容准确性与当前限制见 [测试策略](./TESTING.md)。
