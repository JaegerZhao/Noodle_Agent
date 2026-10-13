from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import machine_learning_blueprint as ml


@dataclass(frozen=True)
class SynthesisBlueprint:
    key: str
    domain_key: str
    concept_namespace: str
    source_note_prefix: str
    source_ref_prefix: str
    synthesis_key: str
    legacy_synthesis_key: str | None
    title: str
    release_title: str
    summary: str
    editor_role: str
    groups: list[dict[str, Any]]
    concepts: list[dict[str, Any]]
    exercise_owner_by_section: dict[str, str]
    excluded_source_prefixes: list[str]
    excluded_title_terms: set[str]

    def __post_init__(self) -> None:
        group_keys = [item["key"] for item in self.groups]
        concept_keys = [item["key"] for item in self.concepts]
        if len(group_keys) != len(set(group_keys)):
            raise ValueError(f"Duplicate group key in {self.key} blueprint")
        if len(concept_keys) != len(set(concept_keys)):
            raise ValueError(f"Duplicate concept key in {self.key} blueprint")
        collisions = set(group_keys) & set(concept_keys)
        if collisions:
            raise ValueError(
                f"Group and Concept keys must be disjoint in {self.key}: {sorted(collisions)}"
            )
        valid_parents = set(group_keys) | set(concept_keys)
        for item in self.concepts:
            if item["parent_key"] not in valid_parents:
                raise ValueError(f"Unknown parent {item['parent_key']} for Concept {item['key']}")
            if item["parent_key"] == item["key"]:
                raise ValueError(f"Concept {item['key']} cannot be its own parent")

    def stable_concept_key(self, spec: dict[str, Any]) -> str:
        return str(spec.get("stable_key") or f"{self.concept_namespace}:{spec['key']}")


MACHINE_LEARNING = SynthesisBlueprint(
    key="machine-learning",
    domain_key="machine-learning",
    concept_namespace="ml",
    source_note_prefix="pml",
    source_ref_prefix="PML",
    synthesis_key="machine-learning-semantic",
    legacy_synthesis_key="machine-learning-foundation",
    title="机器学习知识体系",
    release_title="机器学习知识体系 · 语义 Backbone",
    summary="去除书籍前后置结构，以可复用 Concept 和版本化 Block 组织机器学习知识。",
    editor_role="机器学习课程编辑",
    groups=list(ml.GROUPS),
    concepts=list(ml.CONCEPTS),
    exercise_owner_by_section=dict(ml.EXERCISE_OWNER_BY_SECTION),
    excluded_source_prefixes=list(ml.EXCLUDED_SOURCE_PREFIXES),
    excluded_title_terms=set(ml.EXCLUDED_TITLE_TERMS),
)


DEEP_LEARNING_GROUPS: list[dict[str, Any]] = [
    {"key": "foundations", "title": "深度学习基础", "parent_key": None},
    {"key": "training", "title": "网络训练与泛化", "parent_key": None},
    {"key": "cnn", "title": "卷积神经网络", "parent_key": None},
    {"key": "sequence", "title": "序列建模", "parent_key": None},
    {"key": "attention-and-transformers", "title": "注意力与 Transformer", "parent_key": None},
    {"key": "optimization-and-training", "title": "深度学习优化", "parent_key": None},
    {"key": "systems", "title": "计算系统与工程", "parent_key": None},
    {"key": "vision", "title": "计算机视觉", "parent_key": None},
    {"key": "nlp", "title": "自然语言处理", "parent_key": None},
]


def _concept(
    key: str,
    title: str,
    parent_key: str,
    prefixes: list[str],
    summary: str,
    *,
    kind: str = "COMPOSITE",
    stable_key: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key,
        "title": title,
        "parent_key": parent_key,
        "kind": kind,
        "source_prefixes": prefixes,
        "summary_hint": summary,
    }
    if stable_key:
        row["stable_key"] = stable_key
    return row


DEEP_LEARNING_CONCEPTS: list[dict[str, Any]] = [
    _concept(
        "deep-learning",
        "深度学习",
        "foundations",
        ["1"],
        "深度学习的问题背景、表示学习思想、发展脉络和主要应用。",
    ),
    _concept(
        "tensor-and-data",
        "张量与数据处理",
        "foundations",
        ["2", "2.1", "2.2", "2.7"],
        "张量操作、广播、索引、数据读写和常见预处理。",
    ),
    _concept(
        "linear-algebra-for-ml",
        "机器学习中的线性代数",
        "foundations",
        ["2.3"],
        "向量、矩阵、张量及其在线性模型和神经网络中的运算。",
        stable_key="ml:linear-algebra-for-ml",
    ),
    _concept(
        "calculus-and-autodiff",
        "微积分与自动微分",
        "foundations",
        ["2.4", "2.5"],
        "导数、梯度、链式法则和计算图上的自动微分。",
    ),
    _concept(
        "random-variables",
        "随机变量与概率基础",
        "foundations",
        ["2.6"],
        "概率分布、期望、方差和统计推断所需的概率基础。",
        stable_key="ml:random-variables",
    ),
    _concept(
        "linear-regression",
        "线性回归",
        "foundations",
        ["3", "3.1", "3.2", "3.3"],
        "线性回归的损失、优化、从零实现和框架实现。",
        stable_key="ml:linear-regression",
    ),
    _concept(
        "softmax-regression",
        "Softmax 回归与多分类",
        "foundations",
        ["3.4", "3.5", "3.6", "3.7"],
        "多分类概率模型、交叉熵、图像分类数据和实现方法。",
    ),
    _concept(
        "multilayer-perceptron",
        "多层感知机",
        "training",
        ["4", "4.1", "4.2", "4.3"],
        "隐藏层、激活函数以及多层感知机的实现。",
        stable_key="ml:multilayer-perceptron",
    ),
    _concept(
        "generalization",
        "模型选择与泛化",
        "training",
        ["4.4", "4.9", "4.10"],
        "训练误差、泛化误差、欠拟合、过拟合和分布偏移。",
    ),
    _concept(
        "regularization",
        "正则化",
        "training",
        ["4.5", "4.6"],
        "权重衰减、Dropout 等控制模型复杂度的方法。",
        stable_key="ml:regularization",
    ),
    _concept(
        "backpropagation",
        "反向传播与参数初始化",
        "training",
        ["4.7", "4.8"],
        "前向传播、反向传播、计算图、数值稳定性和初始化。",
        stable_key="ml:backpropagation",
    ),
    _concept(
        "model-engineering",
        "深度学习模型工程",
        "training",
        ["5", "5.1", "5.2", "5.3", "5.4", "5.5", "5.6"],
        "层与块、参数管理、自定义层、模型持久化和 GPU 使用。",
    ),
    _concept(
        "convolutional-neural-networks",
        "卷积神经网络",
        "cnn",
        ["6"],
        "卷积的结构先验、卷积运算、通道、填充、步幅、池化和 LeNet。",
        stable_key="ml:convolutional-neural-networks",
    ),
    _concept(
        "cnn-architectures",
        "经典卷积网络架构",
        "cnn",
        ["7", "7.1", "7.2", "7.3", "7.4", "7.6", "7.7"],
        "AlexNet、VGG、NiN、GoogLeNet、ResNet 与 DenseNet 的设计演进。",
    ),
    _concept(
        "normalization",
        "神经网络归一化",
        "cnn",
        ["7.5"],
        "批量归一化的计算、训练行为和网络集成方式。",
    ),
    _concept(
        "sequence-modeling",
        "序列建模与语言模型",
        "sequence",
        ["8", "8.1", "8.2", "8.3"],
        "时间序列、文本预处理、语言模型和序列数据采样。",
    ),
    _concept(
        "recurrent-neural-networks",
        "循环神经网络",
        "sequence",
        ["8.4", "8.5", "8.6", "8.7"],
        "循环状态、RNN 实现和通过时间反向传播。",
        stable_key="ml:recurrent-neural-networks",
    ),
    _concept(
        "gated-rnns",
        "门控循环神经网络",
        "sequence",
        ["9", "9.1", "9.2"],
        "GRU 与 LSTM 的门控机制、状态更新和实现。",
    ),
    _concept(
        "rnn-variants",
        "深层与双向循环网络",
        "sequence",
        ["9.3", "9.4"],
        "堆叠循环层和双向上下文建模。",
    ),
    _concept(
        "sequence-to-sequence",
        "编码器—解码器与 Seq2Seq",
        "sequence",
        ["9.5", "9.6", "9.7", "9.8"],
        "机器翻译数据、编码器—解码器、Seq2Seq 训练和束搜索。",
    ),
    _concept(
        "attention",
        "注意力机制",
        "attention-and-transformers",
        ["10", "10.1", "10.2", "10.3", "10.4"],
        "注意力提示、评分函数、注意力汇聚和 Bahdanau 注意力。",
        stable_key="ml:attention",
    ),
    _concept(
        "self-attention",
        "多头自注意力与位置编码",
        "attention",
        ["10.5", "10.6"],
        "多头注意力、自注意力和序列位置信息编码。",
    ),
    _concept(
        "transformers",
        "Transformer",
        "attention",
        ["10.7"],
        "Transformer 编码器、解码器、残差连接和训练流程。",
        stable_key="ml:transformers",
    ),
    _concept(
        "optimization",
        "深度学习优化基础",
        "optimization-and-training",
        ["11", "11.1", "11.2", "11.3"],
        "优化与深度学习的关系、凸性和梯度下降。",
        stable_key="ml:optimization",
    ),
    _concept(
        "stochastic-gradient-descent",
        "随机与小批量梯度下降",
        "optimization",
        ["11.4", "11.5"],
        "随机梯度、小批量训练、方差与计算效率。",
        stable_key="ml:stochastic-gradient-descent",
    ),
    _concept(
        "adaptive-optimizers",
        "动量与自适应优化器",
        "optimization",
        ["11.6", "11.7", "11.8", "11.9", "11.10"],
        "Momentum、AdaGrad、RMSProp、Adadelta 和 Adam。",
    ),
    _concept(
        "learning-rate-scheduling",
        "学习率调度",
        "optimization",
        ["11.11"],
        "学习率衰减、预热和调度策略。",
    ),
    _concept(
        "deep-learning-runtime",
        "深度学习运行时与硬件",
        "systems",
        ["12", "12.1", "12.2", "12.3", "12.4"],
        "编译与解释、异步执行、自动并行和硬件特征。",
    ),
    _concept(
        "distributed-training",
        "多 GPU 与分布式训练",
        "systems",
        ["12.5", "12.6", "12.7"],
        "数据并行、多 GPU 实现和参数服务器。",
    ),
    _concept(
        "deep-learning-tooling",
        "深度学习开发工具",
        "systems",
        ["16"],
        "Jupyter、云端环境、服务器与 GPU 选择及 D2L 工具接口。",
    ),
    _concept(
        "vision-transfer-learning",
        "视觉数据增广与迁移学习",
        "vision",
        ["13", "13.1", "13.2"],
        "图像增广、预训练模型和微调。",
        stable_key="ml:transfer-learning",
    ),
    _concept(
        "object-detection",
        "目标检测",
        "vision",
        ["13.3", "13.4", "13.5", "13.6", "13.7", "13.8"],
        "边界框、锚框、多尺度检测、SSD 和 R-CNN 系列。",
    ),
    _concept(
        "semantic-segmentation",
        "语义分割",
        "vision",
        ["13.9", "13.10", "13.11"],
        "像素级预测、转置卷积和全卷积网络。",
    ),
    _concept(
        "neural-style-transfer",
        "神经风格迁移",
        "vision",
        ["13.12"],
        "内容损失、风格损失和图像优化过程。",
    ),
    _concept(
        "vision-practice",
        "计算机视觉实战",
        "vision",
        ["13.13", "13.14"],
        "图像分类竞赛中的数据、训练和工程实践。",
    ),
    _concept(
        "word-embeddings",
        "词嵌入",
        "nlp",
        ["14", "14.1", "14.2", "14.3", "14.4", "14.5", "14.6", "14.7"],
        "Word2Vec、GloVe、子词嵌入、近似训练与评估。",
        stable_key="ml:word-embeddings",
    ),
    _concept(
        "bert",
        "BERT 预训练与微调",
        "nlp",
        ["14.8", "14.9", "14.10", "15.6", "15.7"],
        "BERT 架构、预训练数据与任务、下游微调。",
    ),
    _concept(
        "sentiment-analysis",
        "情感分析",
        "nlp",
        ["15", "15.1", "15.2", "15.3"],
        "情感数据、RNN/CNN 分类模型和训练流程。",
    ),
    _concept(
        "natural-language-inference",
        "自然语言推断",
        "nlp",
        ["15.4", "15.5"],
        "自然语言推断数据、注意力模型和判别流程。",
    ),
]


DEEP_LEARNING = SynthesisBlueprint(
    key="deep-learning",
    domain_key="deep-learning",
    concept_namespace="dl",
    source_note_prefix="d2l",
    source_ref_prefix="D2L",
    synthesis_key="deep-learning-semantic",
    legacy_synthesis_key=None,
    title="深度学习知识体系",
    release_title="深度学习知识体系 · D2L 语义 Backbone",
    summary="以《动手学深度学习》完整 Notes 为来源，按可复用 Concept 与版本化 Block 组织深度学习知识。",
    editor_role="深度学习课程编辑",
    groups=DEEP_LEARNING_GROUPS,
    concepts=DEEP_LEARNING_CONCEPTS,
    exercise_owner_by_section={},
    excluded_source_prefixes=["P", "I", "N", "R"],
    excluded_title_terms=set(),
)


BLUEPRINTS = {
    MACHINE_LEARNING.domain_key: MACHINE_LEARNING,
    DEEP_LEARNING.domain_key: DEEP_LEARNING,
}


def get_blueprint(domain_key: str) -> SynthesisBlueprint:
    try:
        return BLUEPRINTS[domain_key]
    except KeyError as exc:
        raise ValueError(f"No semantic Synthesis blueprint for domain: {domain_key}") from exc
