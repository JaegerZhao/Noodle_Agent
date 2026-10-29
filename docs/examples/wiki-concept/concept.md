---
id: demo_concept_backprop
kind: CONCEPT
revision: demo_concept_backprop_r1
aliases:
  - Backpropagation
tags:
  - machine-learning
---

# 反向传播

> 合成示例，仅用于展示契约和 Markdown 结构，未对应真实课件验收。

通过链式法则计算梯度，并与参数优化区分。

## 反向传播计算什么

反向传播沿计算依赖反向计算梯度；优化方法使用梯度更新参数。两者承担不同职责。[^input_a]

## 为什么需要汇总后续路径

同一变量通过多条路径影响损失时，需要汇总这些路径的导数贡献，再继续反向计算。[^input_b]

[^input_a]: 演示原子笔记 A 的定义章节，实际系统由来源编译器生成链接。
[^input_b]: 演示原子笔记 B 的链式法则章节。
