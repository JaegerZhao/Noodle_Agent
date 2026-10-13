# 贡献指南

欢迎通过 Issue 讨论错误、功能与设计取舍，或提交小范围、可验证的 Pull Request。

## 开发环境

使用 Node.js 24 和 Python 3.12，安装 Node 与 Python 开发依赖：

```sh
npm ci
python -m pip install -e "./services/noodle[dev]"
```

从 `.env.example` 创建本地 `.env`。密钥、数据库、课程 PDF、真实生成笔记、日志与演示档案禁止提交。复现问题使用合成资料，避免在 Issue 中公开个人知识库。

## 提交检查

```sh
npm run check:release
npm run typecheck
npm test
npm run build
python -m ruff check services/noodle/src services/noodle/tests
python -m pytest services/noodle/tests
python scripts/delivery-check.py
```

接口 Schema、工具契约、数据模型与同步策略变更应附对应回归测试和文档。测试覆盖业务风险即可，不需要为纯文案修改增加测试。外部模型调用不进入默认 CI。

## 代码与发布

- `src/` 放 Harness 桥接，Python 领域逻辑放 `services/noodle/src/noodle_agent/`。
- 任务恢复、来源失效和版本冲突属于知识更新的必要约束，不得由重试或模型成功返回绕过。
- 保留第三方许可声明；新增依赖时检查许可兼容性。
- 新增环境变量须更新 `.env.example`，不要加入真实值或个人绝对路径。
- 提交说明描述问题、行为变化和验证方式。推荐 `feat:`、`fix:`、`docs:`、`test:`、`chore:` 前缀。
- npm 安装脚本使用 `allowScripts` 白名单；依赖升级需要检查原生安装脚本。

提交贡献即表示你有权以项目的 AGPL-3.0 许可证提供相应代码。
