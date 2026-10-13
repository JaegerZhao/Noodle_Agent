# 第三方组件与许可

本项目代码以 AGPL-3.0 发布。依赖与内嵌前端组件按其自身许可证提供，用户上传的资料不属于本项目发布内容。

| 组件 | 用途 | 许可证 / 声明 |
| --- | --- | --- |
| DeepSeek Harness / Cordis | 主 Agent、工具和插件基础 | MIT，见安装包许可 |
| PyMuPDF / MuPDF | PDF 操作和示例生成 | AGPL-3.0 或 Artifex 商业许可；当前项目使用开源分发路径 |
| PDF.js | 浏览器 PDF 阅读 | Apache-2.0，`web/vendor/LICENSE.pdfjs.txt` |
| KaTeX 与字体 | 数学渲染 | MIT，`web/vendor/LICENSE.katex.txt` |
| Marked | Markdown 解析 | MIT，`web/vendor/LICENSE.marked.txt` |
| DOMPurify | HTML 清理 | 保留 `web/vendor/LICENSE.dompurify.txt` 的双许可声明 |

上述 `web/` 路径相对于 `services/noodle/src/noodle_agent/`。Node 依赖清单位于 `package.json` 与 `package-lock.json`，Python 依赖位于 `services/noodle/pyproject.toml`。其他依赖及传递依赖的许可证以相应发行包为准。

参考：[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)、[PyMuPDF 许可](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)。外部托管解析、生成与向量服务的 API 使用条款独立于本项目代码许可。
