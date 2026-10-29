"""Material routing and TOC ownership use offline PDFs and explicit source units."""

import fitz

from noodle_agent.next.adaptive_planning import book_boundaries
from noodle_agent.next.document_outline import build_outline
from noodle_agent.next.generation_contracts import Unit
from noodle_agent.next.material_profiles import assess_source


def document(pages=3, landscape=False, toc=None, text="Example"):
    with fitz.open() as pdf:
        for _ in range(pages):
            p = pdf.new_page(width=800 if landscape else 600, height=600 if landscape else 800)
            p.insert_text((40, 40), text)
        if toc:
            pdf.set_toc(toc)
        return pdf.tobytes()


def test_landscape_slides_with_chapter_heading_are_not_a_book():
    content = document(landscape=True, toc=[[1, "1 Classification", 1]])
    profile = assess_source(content, {"blocks": []}, build_outline(content))
    assert profile["kind"] == "SLIDES" and profile["selected_route"] == "SLIDES_TOPICS"


def test_paper_is_not_identified_by_page_count_alone():
    content = document(text="Abstract. A method is presented. References.")
    profile = assess_source(content, {"blocks": []}, build_outline(content))
    assert profile["kind"] == "PAPER"
    unknown = document(text="Unclassified document")
    assert assess_source(unknown, {"blocks": []}, build_outline(unknown))["kind"] == "UNKNOWN"


def test_book_toc_keeps_same_page_sections_and_never_crosses_chapters():
    content = document(90, toc=[[1, "1 Foundations", 1], [2, "1.1 Definitions", 1], [2, "1.2 Examples", 2],
                               [1, "2 Methods", 46], [2, "2.1 Procedure", 46]])
    outline = build_outline(content)
    profile = assess_source(content, {"blocks": []}, outline)
    assert profile["kind"] == "BOOK"
    data = [(1, "1 Foundations", "HEADING"), (1, "1.1 Definitions", "HEADING"), (1, "A definition.", "TEXT"),
            (2, "1.2 Examples", "HEADING"), (2, "A full worked example.", "TEXT"),
            (46, "2 Methods", "HEADING"), (46, "2.1 Procedure", "HEADING"), (46, "Method steps.", "TEXT")]
    units = [Unit(unit_id=f"u{i}", block_id=f"b{i}", page=p, text=t, kind=k, start=0, end=len(t)) for i, (p, t, k) in enumerate(data)]
    result = book_boundaries(units, outline)
    assert len(result.sections) == 2
    assert [s.title for s in result.sections] == ["1 Foundations", "2 Methods"]
    assert result.sections[0].start_unit == "u0" and result.sections[1].start_unit == "u5"
    assert len({s.chapter_key for s in result.sections}) == 2


def test_preface_is_not_discarded_as_pure_front_matter():
    content = document(5, toc=[[1, "Preface", 1], [1, "1 Introduction", 3]])
    units = [Unit(unit_id="a", block_id="a", page=1, text="Motivation and important definitions.", kind="TEXT", start=0, end=37),
             Unit(unit_id="b", block_id="b", page=3, text="Teaching content.", kind="TEXT", start=0, end=17)]
    result = book_boundaries(units, build_outline(content))
    assert all(s.disposition == "CONTENT" for s in result.sections)


def test_appendix_under_a_part_has_its_own_execution_scope():
    content = document(8, toc=[[1, "V Beyond Supervised Learning", 1],
                               [2, "23 Graph Embeddings", 1], [2, "A Notation", 6]])
    outline = build_outline(content)
    data = [(1, "23 Graph Embeddings", "HEADING"), (1, "Graph examples.", "TEXT"),
            (6, "A Notation", "HEADING"), (6, "Notation definitions.", "TEXT")]
    units = [Unit(unit_id=f"u{i}", block_id=f"b{i}", page=p, text=t, kind=k, start=0, end=len(t)) for i, (p, t, k) in enumerate(data)]
    result = book_boundaries(units, outline)
    appendix = next(n for n in outline.nodes if n.kind == "APPENDIX")
    assert result.sections[-1].chapter_key == appendix.node_id
    assert result.sections[0].chapter_key != result.sections[-1].chapter_key


def test_contents_in_front_matter_does_not_discard_actual_introductory_knowledge():
    content = document(5, toc=[[1, "1 Introduction", 3]])
    paragraph = "A learning algorithm estimates a mapping from observed data. " * 8
    data = [(1, "Contents", "HEADING"), (2, paragraph, "TEXT"), (3, "Core material", "TEXT")]
    units = [Unit(unit_id=f"u{i}", block_id=f"b{i}", page=p, text=t, kind=k, start=0, end=len(t)) for i, (p, t, k) in enumerate(data)]
    assert book_boundaries(units, build_outline(content)).sections[0].disposition == "CONTENT"


def test_multiline_contents_and_publisher_acknowledgements_are_navigation():
    content = document(5, toc=[[1, "1 Introduction", 3]])
    contents = "\n".join(f"1.{i} What is example {i}? {i + 5}" for i in range(10))
    acknowledgement = "The publisher thanks the peer reviewers. The quality of our publications depends on them. " * 4
    data = [(1, "Contents", "HEADING"), (1, acknowledgement, "TEXT"), (2, contents, "TEXT"), (3, "Core material", "TEXT")]
    units = [Unit(unit_id=f"u{i}", block_id=f"b{i}", page=p, text=t, kind=k, start=0, end=len(t)) for i, (p, t, k) in enumerate(data)]
    result = book_boundaries(units, build_outline(content))
    assert result.sections[0].disposition == "EXCLUDE"
    assert result.sections[-1].disposition == "CONTENT"
