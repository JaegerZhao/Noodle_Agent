"""Local, version-bound PDF hierarchy. Reading structure never becomes RAG evidence."""

import re
from collections import Counter

import fitz

from .contracts import Contract, canonical, sha
from .planning import QualityFailure

POLICY = "document-outline-v2"


class OutlineNode(Contract):
    node_id: str
    parent_id: str | None = None
    title: str
    kind: str
    level: int
    position: int
    start: int
    end: int
    basis: str = "PDF_BOOKMARK"


class DocumentOutline(Contract):
    policy: str = POLICY
    source_sha256: str
    page_count: int
    nodes: list[OutlineNode]
    segments: list[dict]
    issues: list[dict]


def node_kind(title):
    t = title.strip()
    if re.fullmatch(r"(?:bibliography|references|参考文献|参考资料)", t, re.IGNORECASE):
        return "BIBLIOGRAPHY"
    if re.fullmatch(r"(?:index|subject index|索引)", t, re.IGNORECASE):
        return "INDEX"
    if re.match(r"^(?:preface|foreword|前言|序言|序)(?:\s|$)", t, re.IGNORECASE):
        return "PREFACE"
    if re.match(r"^(?:[IVX]+\s+\S|part\s+|第.+[篇部])", t, re.IGNORECASE):
        return "PART"
    if re.match(
        r"^(?:appendix\b|附录|[A-Z]\s+\S)",
        t,
        re.IGNORECASE if t.lower().startswith("appendix") else 0,
    ):
        return "APPENDIX"
    if re.match(r"^(?:\d+\s+\S|chapter\s+\w+|第.+章)", t, re.IGNORECASE):
        return "CHAPTER"
    return "SECTION"


def build_outline(content, blocks=None):
    issues = []
    with fitz.open(stream=content, filetype="pdf") as pdf:
        count = len(pdf)
        entries = []
        page_headings = None
        for index, (level, title, page) in enumerate(pdf.get_toc()):
            if not 1 <= page <= count:
                issues.append(
                    {"code": "INVALID_BOOKMARK_PAGE", "title": title, "severity": "ERROR"}
                )
                continue
            kind = node_kind(title)
            basis = "PDF_BOOKMARK"
            # Some publishers point both Index and Bibliography at the appendix.
            # Correct only exact, short page-heading matches; never guess from body mentions.
            if kind in {"BIBLIOGRAPHY", "INDEX"}:
                if page_headings is None:
                    page_headings = [
                        {
                            re.sub(r"\s+", " ", line).strip().casefold()
                            for line in p.get_text().splitlines()[:8]
                        }
                        for p in pdf
                    ]
                expected = re.sub(r"\s+", " ", title).strip().casefold()
                if expected not in page_headings[page - 1]:
                    matches = [i + 1 for i, lines in enumerate(page_headings) if expected in lines]
                    if matches:
                        corrected = min(matches, key=lambda p: abs(p - page))
                        issues.append(
                            {
                                "code": "CORRECTED_BOOKMARK_PAGE",
                                "title": title,
                                "from": page,
                                "to": corrected,
                                "severity": "INFO",
                            }
                        )
                        page, basis = corrected, "VERIFIED_PAGE_HEADING"
                    else:
                        issues.append(
                            {
                                "code": "UNVERIFIED_BACKMATTER_BOUNDARY",
                                "title": title,
                                "severity": "ERROR",
                            }
                        )
            entries.append((int(level), str(title), int(page), kind, basis, index))
        if not entries and blocks:
            for index, b in enumerate(blocks):
                title = b.get("text", "").strip()
                if b.get("kind", "").upper() in {"HEADING", "TITLE"} and node_kind(title) in {
                    "CHAPTER",
                    "APPENDIX",
                    "BIBLIOGRAPHY",
                    "INDEX",
                }:
                    entries.append((1, title, b["page"], node_kind(title), "PARSED_HEADING", index))
        if not entries:
            entries = [(1, "全部内容", 1, "DOCUMENT", "FALLBACK", 0)]
            if count > 180:
                issues.append(
                    {
                        "code": "LONG_DOCUMENT_NEEDS_OUTLINE",
                        "severity": "ERROR",
                        "title": "未找到可核对的长文档目录",
                    }
                )
        entries.sort(key=lambda e: e[5])
        if entries[0][2] > 1:
            entries.insert(0, (1, "前置内容", 1, "FRONT", "SYNTHETIC_PREFIX", -1))
        nodes, stack = [], []
        for position, (level, title, page, kind, basis, _) in enumerate(entries):
            while stack and stack[-1]["level"] >= level:
                stack.pop()
            parent = stack[-1]["node_id"] if stack else None
            if nodes and page < nodes[-1]["start"]:
                issues.append(
                    {"code": "NON_MONOTONIC_BOOKMARK", "title": title, "severity": "ERROR"}
                )
            node = {
                "node_id": "outline_" + sha(canonical([position, level, title, page]))[:20],
                "parent_id": parent,
                "title": title,
                "kind": kind,
                "level": level,
                "position": position,
                "start": page,
                "end": count,
                "basis": basis,
            }
            nodes.append(node)
            stack.append(node)
        for i, node in enumerate(nodes):
            for following in nodes[i + 1 :]:
                if following["level"] <= node["level"]:
                    node["end"] = max(node["start"], following["start"] - 1)
                    break
        major = [n for n in nodes if n["kind"] != "SECTION"]
        starts = {}
        for n in major:
            # A chapter beginning on a part's title page owns that page's content.
            if (
                n["start"] in starts
                and n["kind"] != "PART"
                and starts[n["start"]]["kind"] != "PART"
            ):
                issues.append(
                    {"code": "AMBIGUOUS_MAJOR_BOUNDARY", "title": n["title"], "severity": "ERROR"}
                )
            if n["start"] not in starts or n["kind"] != "PART":
                starts[n["start"]] = n
        if 1 not in starts:
            starts[1] = nodes[0]
        ordered = sorted(starts.items())
        segments = [
            {
                "node_id": n["node_id"],
                "title": n["title"],
                "kind": n["kind"],
                "start": start,
                "end": ordered[i + 1][0] - 1 if i + 1 < len(ordered) else count,
            }
            for i, (start, n) in enumerate(ordered)
        ]
    return DocumentOutline(
        source_sha256=sha(content),
        page_count=count,
        nodes=[OutlineNode(**n) for n in nodes],
        segments=segments,
        issues=issues,
    )


def placement(outline, pages):
    """Use the deepest unambiguous common ancestor; page ties never invent section precision."""
    by_id = {n.node_id: n for n in outline.nodes}
    owners = []
    for page in sorted(set(pages)):
        candidates = [n for n in outline.nodes if n.start <= page <= n.end]
        leaves = [
            n for n in candidates if not any(other.parent_id == n.node_id for other in candidates)
        ]
        if not leaves:
            continue
        paths = []
        for leaf in leaves:
            chain = [leaf.node_id]
            while by_id[chain[-1]].parent_id:
                chain.append(by_id[chain[-1]].parent_id)
            paths.append(list(reversed(chain)))
        shared = [v for v in paths[0] if all(v in path for path in paths)]
        owners.append(shared[-1] if shared else leaves[0].node_id)
    if not owners:
        return None, []
    chains = []
    for owner in owners:
        chain = [owner]
        while by_id[chain[-1]].parent_id:
            chain.append(by_id[chain[-1]].parent_id)
        chains.append(list(reversed(chain)))
    shared = [v for v in chains[0] if all(v in path for path in chains)]
    return (shared[-1] if shared else None), list(dict.fromkeys(owners))


def check_book_coverage(units, plans):
    counts = Counter()
    for plan in plans:
        for topic in plan["topics"]:
            counts.update(topic["units"])
        counts.update(e["unit_id"] for e in plan["coverage"]["exclusions"])
    if set(counts) != {u.unit_id for u in units} or any(n != 1 for n in counts.values()):
        raise QualityFailure("Book plan must assign or exclude every original unit exactly once")


def unit_node_map(outline, units, *, reading_boundary=False):
    """Resolve same-page headings by source-unit order, not page-number guesses."""

    def normalized(value):
        return re.sub(r"[^\w]", "", re.sub(r"<[^>]+>", "", value).casefold())

    by_title = {}
    by_id = {n.node_id: n for n in outline.nodes}
    for n in outline.nodes:
        by_title.setdefault(normalized(n.title), []).append(n)
    current = None
    result = {}
    for unit in units:
        matches = (
            by_title.get(normalized(unit.text), [])
            if unit.kind.upper() in {"TITLE", "HEADING"}
            else []
        )
        matches = [n for n in matches if abs(n.start - unit.page) <= 1]
        if reading_boundary and current:
            ancestors = set()
            parent = by_id[current].parent_id
            while parent:
                ancestors.add(parent)
                parent = by_id[parent].parent_id
            # MinerU sometimes labels a repeated running header as HEADING.
            # A later-page parent header must not pull a child's body back out
            # of its source subsection. Frozen planning keeps its old policy.
            matches = [n for n in matches if not (n.node_id in ancestors and unit.page > n.start)]
        if matches:
            current = min(matches, key=lambda n: abs(n.start - unit.page)).node_id
        elif current is None or not by_id[current].start <= unit.page <= by_id[current].end + int(reading_boundary):
            current = placement(outline, [unit.page])[0]
        result[unit.unit_id] = current
    return result


def placement_for_units(outline, unit_ids, owners, pages):
    contexts = list(dict.fromkeys(owners[u] for u in unit_ids if owners.get(u)))
    if not contexts:
        return placement(outline, pages)
    by_id = {n.node_id: n for n in outline.nodes}
    chains = []
    for owner in contexts:
        chain = [owner]
        while by_id[chain[-1]].parent_id:
            chain.append(by_id[chain[-1]].parent_id)
        chains.append(list(reversed(chain)))
    shared = [v for v in chains[0] if all(v in chain for chain in chains)]
    return (shared[-1] if shared else None), contexts
