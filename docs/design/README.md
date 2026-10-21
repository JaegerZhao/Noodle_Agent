# 新系统分阶段设计

日期：2026-10-21。P2 后端功能已收尾；P3、P4 后端已实现并完成工程与小样本真实验收，完整新前端仍属 P5。实际运行结果见 [实施记录](../IMPLEMENTATION-STATUS.md)。

| 文档 | 内容 |
| --- | --- |
| [P3：Concept 维护](./P3-CONCEPT-MAINTENANCE.md) | 片段范围、身份/章节/增量判断、八类动作、局部综合、删除修改、事件并发与验收 |
| [P4：知识检索与问答](./P4-KNOWLEDGE-RAG.md) | Chunk、存储、精确向量与 Chroma 对照、FTS/RRF/重排、范围、引用与评测 |

## 设计合同和示例

- [Concept 更新计划 Schema](../schemas/planned-concept-update.schema.json) · [示例](./examples/concept-update.json)
- [RAG 查询 Schema](../schemas/planned-rag-query.schema.json) · [全库示例](./examples/rag-query.json) · [单 PDF 示例](./examples/rag-source-query.json)
- [RAG 回答 Schema](../schemas/planned-rag-response.schema.json) · [示例](./examples/rag-response.json)

这些文件使用 `planned-*` 标记，与现有运行时合同分开。示例 ID 与哈希为结构占位；Schema 不证明语义支持、权限、来源有效性或事务正确性。

## 建议实施顺序

先 P3-A 统一事件路由与片段清单，再做身份与八动作、章节构建和来源同步。P4 可先用已有有效原子笔记建立基线，Concept 正式发布后统一接入；完成索引与问答后再进行 Chroma 对照。完整新版前端和 Vault 属于 P5。

每个批次都有独立验收。已运行新的 Concept 维护及真实 RAG 问答。实际行为见 [使用说明](../P3-P4-IMPLEMENTATION.md)、[验收记录](../P3-P4-ACCEPTANCE.md)；[运行时 Schema](../schemas/next-v1-p3p4.schema.json) 与本目录的 planned-* 草案分开保存。
