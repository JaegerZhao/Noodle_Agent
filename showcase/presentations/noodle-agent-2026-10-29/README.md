# Noodle Agent HTML 演示

白橙配色，16:9 固定画布（1920 × 1080），自动按浏览器窗口等比缩放。共 26 页：22 页主讲、4 页追问附录。所有图、脚本和样式均保存在本目录，可离线打开。

## 打开与操作

打开 `index.html`，或在本目录运行 `python -m http.server 8770 --bind 127.0.0.1`，访问 `http://127.0.0.1:8770/index.html`。

- ← / →、空格：翻页；Home / End：首尾页。
- O：总览并跳转到指定页面。
- N：当前页讲稿抽屉。
- S：演讲者窗口，需要浏览器允许弹窗；不支持弹窗时使用 N。
- F：全屏，是否允许由浏览器决定。
- 点击流程图：放大查看；Esc 关闭。
- 鼠标移动时出现底部控制栏，静止后自动收起。

详细讲稿另存于 `speaker-notes.md`。建议简短介绍按第 1、2、3、4、5、8、11、14、17、20、21、22 页讲述，根据时间跳过细节页；完整演示按顺序使用。

## 本轮优化

封面以资料、Atomic、Concept 和引用回答呈现项目主线；内容页采用针对内容的模块对比、步骤展示、分层结构和数据条形图。章节、标题、正文和注释使用统一字号层级。保留原有七张纯流程图，不向图中增加标题或页脚。

原第 7、10、18、25 页引用的截图文件不存在。本版将其改成内容结构、来源支持、引用定位与就绪状态的机制示意，明确其为说明内容，没有伪造实际截图。后续将相应截图放入 `screenshots/` 并重新构建，可以恢复实际截图页面。

## 重新构建

在项目根目录执行：

```powershell
python showcase/presentations/noodle-agent-2026-10-29/author_content.py
python showcase/presentations/noodle-agent-2026-10-29/build_deck.py
```

`author_content.py` 保留原始讲述内容，`polish_content.py` 管理演示布局与缺图替代页，`build_deck.py` 使用 HTML PPT 的 presenter-mode-reveal 模板生成演示及讲稿。`refined.css` 管理本版视觉优化；资源内容 hash 防止浏览器沿用旧样式。

依据：`docs/PROJECT-INTRODUCTION.md`、同日流程图、当前代码和已有验收记录。本轮验证 HTML 展示，未重新执行知识生成或模型质量评测。页面边界及资源检查见 `qa/verification.json`。
