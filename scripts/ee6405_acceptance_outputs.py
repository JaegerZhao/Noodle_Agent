"""Final snapshot checks: real hybrid retrieval/rerank/answers and portable files."""

import io
import json
import posixpath
import re
import subprocess
import time
import zipfile
from pathlib import Path

from noodle_agent.adapters.llm import OpenAIKnowledgeAgent
from noodle_agent.next.audit import ProviderAudit
from noodle_agent.next.checkpoints import Checkpoints
from noodle_agent.next.contracts import Contract, canonical, sha
from noodle_agent.next.eligibility import eligible
from noodle_agent.next.events import dispatch
from noodle_agent.next.exports import article, article_zip
from noodle_agent.next.model_calls import Calls
from noodle_agent.next.navigation import current_notes
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.repository import identifier, one
from noodle_agent.next.retrieval.answering import Answerer
from noodle_agent.next.retrieval.reranking import reranker_from_env
from noodle_agent.next.retrieval.search import Searcher
from noodle_agent.next.vault import portable_files, reading_zip
from noodle_agent.next.worker import keep_lease
from pydantic import Field

CASES = [
    ("逆文档频率如何平滑以避免除零？", ["对文档频率分母作加一平滑以避免零分母"]),
    (
        "NMF 中 min_df=10 与 n_components=10 分别表示什么？",
        ["min_df=10是词至少出现在10篇文档中", "n_components=10是10个潜在主题或分量"],
    ),
    (
        "data_words 是怎样构造的，为什么需要去停用词？",
        [
            "data_words来自文本预处理或分词步骤",
            "停用词由相应移除函数过滤，减少常见低信息词",
        ],
    ),
    (
        "PCA 示例经过 transpose 后样本和特征轴是什么？",
        ["转置后每行词项作为样本", "每列推文作为特征，不能沿用转置前的轴含义"],
    ),
    (
        "截断 SVD 的 U、S、V 转置矩阵分别保留哪些部分？",
        ["U保留前k列", "S保留k乘k奇异值子矩阵", "V转置保留前k行"],
    ),
    (
        "BM25 中 b 和 k1 分别控制什么？",
        ["k1控制词频饱和", "b控制文档长度归一化或惩罚的程度"],
    ),
]

FACT_POLICY = "set-spans-v3"
ANSWER_POLICY = "answer-facts-v4"


class EvidenceQuote(Contract):
    chunk_id: str
    span_ids: list[str] = Field(min_length=1)


class FactCheck(Contract):
    support: dict[str, list[EvidenceQuote]] = Field(
        default_factory=dict,
        description="Keys must be EXACT keys from expected_facts, e.g. 0 and 1, never fact text",
    )
    issues: list[str] = Field(default_factory=list)
    irrelevant_chunk_ids: list[str] = Field(default_factory=list)


class AnswerFactCheck(Contract):
    passed: bool
    supported_fact_ids: list[str]
    issues: list[str] = Field(default_factory=list)


class PairCheck(Contract):
    modes: dict[str, FactCheck]


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def final_outputs(root, store, settings, app):
    head, manifest, _ = app.indexer.snapshot("HYBRID")
    if not head:
        raise QualityFailure("Missing final hybrid index")
    with store.db.engine.connect() as c:
        valid = {n["revision_id"] for n in eligible(c)}
    indexed = {h["revision_id"] for h in manifest}
    if indexed != valid:
        raise QualityFailure(
            "Index does not cover exactly the valid Atomic/Concept revisions"
        )
    final_file = root / (
        "final-check-" + FACT_POLICY + "-" + ANSWER_POLICY + "-" + head["id"] + ".json"
    )
    if final_file.exists():
        cached = json.loads(final_file.read_text(encoding="utf-8"))
        if cached["passed"]:
            return cached
    lease = store.jobs.claim("staged-rag", kind="STAGED_RETRIEVAL_CHECK")
    if lease is None:
        with store.repository.transaction() as c:
            pending = one(
                c,
                "SELECT id FROM events e WHERE event_type='STAGED_RETRIEVAL_CHECK' AND status='QUEUED' AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.event_id=e.id) ORDER BY rowid LIMIT 1",
            )
            if pending:
                event_id = pending["id"]
            else:
                key = "staged-final:" + identifier()
                store.repository.event(
                    c, "STAGED_RETRIEVAL_CHECK", "acceptance", None, {}, key=key
                )
                event_id = one(c, "SELECT id FROM events WHERE dedupe_key=:k", k=key)[
                    "id"
                ]
        store.jobs.enqueue(event_id, "STAGED_RETRIEVAL_CHECK", {})
        lease = store.jobs.claim("staged-rag", kind="STAGED_RETRIEVAL_CHECK")
    audit = ProviderAudit(store.repository, lease)
    app.indexer.embedding.telemetry = audit
    app.indexer.embedding.operation = "acceptance.query_embedding"
    calls = Calls(
        store,
        OpenAIKnowledgeAgent(settings, telemetry=audit),
        Checkpoints(store.repository, lease),
    )
    search = Searcher(app.indexer, reranker_from_env(store=store, provider="dashscope"))
    checks = []
    answers = []
    old_cases = (
        json.loads((root / "retrieval-check.json").read_text(encoding="utf-8"))
        if (root / "retrieval-check.json").exists()
        else []
    )

    def evidence_signature(results):
        return sha(
            canonical(
                {
                    mode: sorted(
                        (h["chunk_id"], h["revision_id"], sha(h["body"]))
                        for h in result["hits"]
                    )
                    for mode, result in results.items()
                }
            )
        )

    try:
        with keep_lease(store.jobs, lease):
            for index, (query, facts) in enumerate(CASES):
                path = root / "rag-cases" / (head["id"] + "-" + str(index) + ".json")
                prior = (
                    json.loads(path.read_text(encoding="utf-8"))
                    if path.exists()
                    else None
                )
                if (
                    prior
                    and prior.get("evaluation_policy") == FACT_POLICY
                    and prior["rerank_passed"]
                ):
                    checks.append(prior)
                    continue
                started = time.perf_counter()
                search_path = path.with_name("search-" + path.name)
                frozen_search = (
                    json.loads(search_path.read_text(encoding="utf-8"))
                    if search_path.exists()
                    else None
                )
                # The index is immutable: reuse real search outputs, not a second rerank request.
                results = (
                    prior["results"]
                    if prior
                    else frozen_search
                    if frozen_search is not None
                    else {
                        mode: search.search(
                            {"query": query, "top_k": 3, "rerank": mode}
                        )
                        for mode in ["OFF", "REQUIRED"]
                    }
                )
                dump(search_path, results)
                passages = {
                    h["chunk_id"]: h["body"]
                    for result in results.values()
                    for h in result["hits"]
                }
                passage_spans = {
                    chunk_id: {
                        "S" + str(i): m.group()
                        for i, m in enumerate(
                            re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text)
                        )
                    }
                    for chunk_id, text in passages.items()
                }
                modes = {
                    mode: [h["chunk_id"] for h in result["hits"]]
                    for mode, result in results.items()
                }

                def checked_support(
                    value, modes=modes, facts=facts, passage_spans=passage_spans
                ):
                    if set(value.modes) != set(modes):
                        raise QualityFailure("Fact judge missed retrieval modes")
                    expected = {str(i) for i in range(len(facts))}
                    for mode, check in value.modes.items():
                        if not set(check.support) <= expected:
                            raise QualityFailure(
                                "Unknown expected fact index: expected="
                                + canonical(sorted(expected))
                                + " received="
                                + canonical(list(check.support))
                            )
                        for quotes in check.support.values():
                            if not quotes:
                                raise QualityFailure(
                                    "Fact support must select source spans"
                                )
                            for quote in quotes:
                                if quote.chunk_id not in modes[mode] or not set(
                                    quote.span_ids
                                ) <= set(passage_spans[quote.chunk_id]):
                                    raise QualityFailure(
                                        "Select existing span_ids belonging to a chunk in this mode"
                                    )

                reusable = next(
                    (
                        c
                        for c in old_cases
                        if c["query"] == query
                        and c.get("evaluation_policy") == FACT_POLICY
                        and c["rerank_passed"]
                        and evidence_signature(c["results"])
                        == evidence_signature(results)
                    ),
                    None,
                )
                if reusable:
                    judged = PairCheck.model_validate(reusable["judgment"])
                    checked_support(judged)
                else:
                    judged = calls.run(
                        "acceptance.retrieval_fact_check",
                        "按每种模式的整个命中集合评估expected_facts的覆盖，不要求每条命中单独覆盖全部事实。support将已覆盖事实的零起始索引字符串映射到实际支持的chunk_id和span_ids，可组合不同片段支持同一事实。只选择提供的段落ID，由程序提取逐字原文，无须抄写或改写。未覆盖事实不要填support。issues只写集合整体缺失的事实或实际相互矛盾，不因某条额外命中无关而阻断；无关项单列irrelevant_chunk_ids。合理同义表达与必然逻辑等价可接受，不要求额外警告措辞；样本沿行、特征沿列与转置后实际行列标签可组合推得样本和特征是什么。不能用外部记忆补事实或混合不兼容约定。modes必须含OFF和REQUIRED。",
                        {
                            "question": query,
                            "expected_facts": {
                                str(i): fact for i, fact in enumerate(facts)
                            },
                            "modes": modes,
                            "passage_spans": passage_spans,
                        },
                        PairCheck,
                        check=checked_support,
                    )
                expected = {str(i) for i in range(len(facts))}
                passed = {
                    mode: result["status"] == "READY"
                    and not judged.modes[mode].issues
                    and set(judged.modes[mode].support) == expected
                    for mode, result in results.items()
                }
                if prior:
                    with (path.with_suffix(".attempts.jsonl")).open(
                        "a", encoding="utf-8"
                    ) as f:
                        f.write(canonical(prior) + "\n")
                item = {
                    "evaluation_policy": FACT_POLICY,
                    "fact_check_reused_for_identical_evidence": reusable is not None,
                    "query": query,
                    "expected_facts": facts,
                    "off_passed": passed["OFF"],
                    "rerank_passed": passed["REQUIRED"],
                    "wall_s": time.perf_counter() - started,
                    "judgment": judged.model_dump(),
                    "quoted_evidence": {
                        mode: {
                            fact: [
                                {
                                    "chunk_id": q.chunk_id,
                                    "quotes": [
                                        passage_spans[q.chunk_id][sid]
                                        for sid in q.span_ids
                                    ],
                                }
                                for q in quotes
                            ]
                            for fact, quotes in check.support.items()
                        }
                        for mode, check in judged.modes.items()
                    },
                    "results": results,
                }
                dump(path, item)
                checks.append(item)
            answerer = Answerer(search, calls)
            old_answers = (
                json.loads((root / "answer-check.json").read_text(encoding="utf-8"))
                if (root / "answer-check.json").exists()
                else []
            )
            for index in [3, 5]:
                query = CASES[index][0]
                cached = next(
                    (
                        a
                        for a in old_answers
                        if a["query"] == query
                        and a["passed"]
                        and a.get("answer_policy") == ANSWER_POLICY
                        and a["result"]["snapshot"]["index_generation"] == head["id"]
                        and search.current(a["result"]["hits"])
                    ),
                    None,
                )
                if cached:
                    answers.append(cached)
                    continue
                response = answerer.answer(
                    {"query": query, "top_k": 3, "rerank": "REQUIRED"}
                )
                passed = (
                    response["status"] == "READY"
                    and bool(response["answer_markdown"])
                    and bool(response["citations"])
                )
                facts = {str(i): fact for i, fact in enumerate(CASES[index][1])}
                fact_check = calls.run(
                    "acceptance.answer_fact_check",
                    "Independently evaluate the final answer, not just whether its citations exist. Every expected fact must be clearly answered with the correct interpretation. Flag internal contradictions, wrong roles, and copying ambiguous terminology without resolving it. Do not infer that the answer states an object simply because the reader could derive it from other statements. Return supported_fact_ids using exact expected_facts keys. A scientific example must distinguish original semantic roles from actual algorithm input roles after operations. passed requires all expected facts and no issues.",
                    {
                        "question": query,
                        "expected_facts": facts,
                        "answer": response["answer_markdown"],
                    },
                    AnswerFactCheck,
                    use_pro_model=True,
                )
                passed = (
                    passed
                    and fact_check.passed
                    and not fact_check.issues
                    and set(fact_check.supported_fact_ids) == set(facts)
                )
                answers.append(
                    {
                        "query": query,
                        "fact_check": fact_check.model_dump(),
                        "answer_policy": ANSWER_POLICY,
                        "passed": passed,
                        "result": response,
                    }
                )
            dump(root / "retrieval-check.json", checks)
            if old_answers and old_answers != answers:
                with (root / "answer-check.attempts.jsonl").open(
                    "a", encoding="utf-8"
                ) as f:
                    f.write(canonical(old_answers) + "\n")
            dump(root / "answer-check.json", answers)
        store.jobs.finish(
            lease,
            {
                "retrieval_passed": sum(c["rerank_passed"] for c in checks),
                "answers_passed": sum(c["passed"] for c in answers),
            },
        )
    except Exception as exc:
        store.jobs.fail(
            lease, {"code": type(exc).__name__, "detail": str(exc)}, retry=False
        )
        raise
    dispatch(store)
    files = portable_files(store)
    missing = []
    image_count = 0
    for name, data in files.items():
        if not name.endswith(".md"):
            continue
        text = data.decode()
        for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
            image_count += 1
            if (
                posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                not in files
            ):
                missing.append([name, target])
        for target in re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", text):
            if target.split("#")[0] + ".md" not in files:
                missing.append([name, target])
    manifest_hash = sha(files[".noodle-manifest.json"])[:16]
    folder = root / ("obsidian-" + manifest_hash)
    for name, data in files.items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder.resolve()):
            raise QualityFailure("Unsafe export path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "obsidian.zip").write_bytes(reading_zip(store))
    source_map = json.loads(
        (root / "generation-result.json").read_text(encoding="utf-8")
    )["result"]["source_map"]["note_id"]
    (root / "source-article.md").write_text(
        article(store, source_map)[0], encoding="utf-8"
    )
    archive = article_zip(store, source_map)
    (root / "source-article.zip").write_bytes(archive)
    article_folder = root / ("reading-" + sha(archive)[:16])
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        for name in package.namelist():
            path = (article_folder / name).resolve()
            if not path.is_relative_to(article_folder.resolve()):
                raise QualityFailure("Unsafe article export path")
            if not name.endswith("/"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(package.read(name))
    article_text = (article_folder / "article.md").read_text(encoding="utf-8")
    for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", article_text):
        if not (article_folder / target).is_file():
            missing.append(["article.md", target])
    notes = [
        {
            "id": n["id"],
            "title": n["title"],
            "kind": n["kind"],
            "markdown": store.get_note(n["id"])["markdown"],
        }
        for n in current_notes(store, limit=1000)["items"]
        if n["kind"] != "MOC"
    ]
    dump(root / "notes-for-render-check.json", notes)
    subprocess.run(
        ["node", str(Path(__file__).with_name("validate-katex-notes.mjs")), str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    math = json.loads((root / "math-render-check.json").read_text(encoding="utf-8"))
    result = {
        "passed": all(c["rerank_passed"] for c in checks)
        and all(a["passed"] for a in answers)
        and not missing
        and not math["errors"],
        "index_generation": head["id"],
        "indexed_notes": len(indexed),
        "chunks": len(manifest),
        "off_passed": sum(c["off_passed"] for c in checks),
        "rerank_passed": sum(c["rerank_passed"] for c in checks),
        "retrieval_cases": len(checks),
        "answers_passed": sum(a["passed"] for a in answers),
        "answer_cases": len(answers),
        "export_folder": str(folder),
        "readable_article": str(article_folder / "article.md"),
        "export_files": len(files),
        "image_occurrences": image_count,
        "missing_links": missing,
        "math_expressions": math["checked"],
        "math_errors": len(math["errors"]),
    }
    dump(final_file, result)
    dump(root / "final-output-check.json", result)
    if not result["passed"]:
        raise QualityFailure(
            "Final retrieval/answer/export check failed: " + canonical(result)
        )
    return result
