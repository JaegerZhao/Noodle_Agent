"""Compact identity retrieval followed by exact target-section comparison."""

import json
import re

from pydantic import Field

from ..contracts import Contract, Id, canonical, sha
from ..planning import QualityFailure


class IdentityMatch(Contract):
    concept_id: Id | None = None
    reason: str = Field(min_length=1)


def brief(item):
    d = item.get("definition", {})
    return {
        "id": item["id"],
        "title": item["title"],
        "domain": item["domain"],
        "aliases": list(dict.fromkeys(item["aliases"] + d.get("aliases", []))),
        "description": item.get("summary") or d.get("description", ""),
        "identity_key": d.get("identity_key"),
        "question": d.get("question"),
        "scope": item.get("scope", d.get("scope")),
    }


def terms(text):
    text = text.casefold()
    return set(re.findall(r"[a-z0-9_]+", text)) | {
        text[i : i + 2]
        for i in range(len(text) - 1)
        if all("\u4e00" <= c <= "\u9fff" for c in text[i : i + 2])
    }


def card(item):
    return {
        **brief(item),
        "valid": item.get("valid", False),
        "sections": [
            {k: s[k] for k in ("section_id", "title", "question", "planned") if k in s}
            for s in item["sections"]
        ],
    }


class IdentityRetriever:
    def __init__(self, store, embedding):
        self.store, self.embedding = store, embedding

    def rank(self, catalogue, query):
        from ..retrieval.vectors import normalize, profile_for

        profile = profile_for(self.embedding, False)
        texts = [canonical(brief(i)) for i in catalogue] + [query]
        vectors = [None] * len(texts)
        missing = []
        for n, t in enumerate(texts):
            ref = f"identity-vectors/{profile['id']}/{sha(t)}.json"
            if self.store.path(ref).exists():
                payload = json.loads(self.store.path(ref).read_text(encoding="utf-8"))
                if payload["text_sha256"] != sha(t):
                    raise QualityFailure("Identity cache binding mismatch")
                vectors[n] = payload["vector"]
            else:
                missing.append((n, ref))
        for start in range(0, len(missing), 10):
            batch = missing[start : start + 10]
            result = self.embedding.embed([texts[n] for n, _ in batch])
            if len(result) != len(batch):
                raise QualityFailure("Identity embedding count mismatch")
            for (n, ref), v in zip(batch, result, strict=True):
                vectors[n] = v
                self.store.put(ref, canonical({"text_sha256": sha(texts[n]), "vector": v}).encode())
        matrix = normalize(vectors, profile["dimensions"])
        scores = matrix[:-1] @ matrix[-1]
        return sorted(range(len(catalogue)), key=lambda n: (-float(scores[n]), catalogue[n]["id"]))


def ranked(catalogue, fragment, retriever=None):
    query = " ".join(
        str(fragment.get(k, ""))
        for k in ("title", "domain", "section_title", "section_question", "text")
    )
    q = terms(query)
    lex = sorted(
        range(len(catalogue)),
        key=lambda n: (-len(q & terms(canonical(card(catalogue[n])))), catalogue[n]["id"]),
    )
    if not retriever or not catalogue:
        return [catalogue[i] for i in lex]
    semantic = retriever.rank(catalogue, query)
    scores = {i: 0.0 for i in lex}
    for ranking in (lex, semantic):
        for rank, i in enumerate(ranking):
            scores[i] += 1 / (30 + rank)
    return [catalogue[i] for i in sorted(scores, key=lambda i: (-scores[i], catalogue[i]["id"]))]


def candidates(catalogue, fragment, budget=14000, retriever=None):
    selected = []
    size = 2
    for item in ranked(catalogue, fragment, retriever)[:6]:
        value = card(item)
        cost = len(canonical(value)) + 1
        if size + cost > budget:
            break
        selected.append(value)
        size += cost
    return selected, len(selected) == len(catalogue)


def comparison(catalogue, decision, fragment, budget=18000):
    target = next((i for i in catalogue if i["id"] == decision.concept_id), None)
    if not target:
        return []
    selected = {
        s["section_id"] for s in target["sections"] if s["section_id"] == decision.section_id
    }
    q = terms(fragment["text"] + " " + fragment.get("section_question", ""))
    ordered = sorted(
        target["sections"],
        key=lambda s: (
            s["section_id"] not in selected,
            -len(q & terms(s.get("question", "") + " " + s.get("markdown", ""))),
        ),
    )
    sections = []
    size = 0
    for s in ordered[:3]:
        cost = len(canonical(s))
        if size + cost > budget:
            raise QualityFailure("Target comparison exceeds context budget; split contribution")
        sections.append(s)
        size += cost
    return [{**brief(target), "valid": target.get("valid", False), "sections": sections}]


def audit_identity(calls, proposal, catalogue, budget=18000, retriever=None):
    if not catalogue:
        return None, {"checked": 0, "total": 0, "all_pages_checked": True}
    f = {"text": proposal.question, "title": proposal.title, "domain": proposal.domain}
    selected = []
    size = 2
    for item in ranked(catalogue, f, retriever)[:20]:
        v = brief(item)
        cost = len(canonical(v)) + 1
        if size + cost > budget:
            break
        selected.append(v)
        size += cost
    allowed = {i["id"] for i in selected}

    def validate(value):
        if value.concept_id is not None and value.concept_id not in allowed:
            raise QualityFailure("Unknown identity match")

    result = calls.run(
        "next.concept_identity_audit",
        "比较领域、核心问题、对象与条件，核查新身份是否与候选等价。名字不同也可同义，同名不同条件不可强并。只返回精确ID；不确定等价时在reason明确说明需要复核。目录为检索候选，不代表全库不存在其他身份。",
        {"proposal": proposal.model_dump(), "identities": selected},
        IdentityMatch,
        check=validate,
    )
    if result.concept_id is None and any(
        t in result.reason for t in ("需要复核", "无法判断", "不确定是否")
    ):
        raise QualityFailure("Uncertain Concept identity needs review: " + result.reason)
    return result.concept_id, {
        "checked": len(selected),
        "total": len(catalogue),
        "all_pages_checked": len(selected) == len(catalogue),
        "method": "hybrid-cards" if retriever else "lexical-cards",
        "reason": result.reason,
    }
