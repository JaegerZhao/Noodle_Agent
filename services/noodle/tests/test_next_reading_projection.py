"""Same-page source headings must not pull preceding text into the next section."""

from noodle_agent.next.document_outline import DocumentOutline, OutlineNode, unit_node_map
from noodle_agent.next.generation_contracts import Unit


def test_reading_keeps_boundary_page_text_until_actual_next_heading():
    outline = DocumentOutline(source_sha256='a' * 64, page_count=3, nodes=[
        OutlineNode(node_id='first', parent_id=None, title='1.1 Classification', kind='SECTION', level=1,
                    position=0, start=1, end=1, basis='PDF_TOC'),
        OutlineNode(node_id='second', parent_id=None, title='1.2 Regression', kind='SECTION', level=1,
                    position=1, start=2, end=3, basis='PDF_TOC')], segments=[], issues=[])
    def unit(key, text, page, kind='TEXT'):
        return Unit(unit_id=key, block_id=key, page=page, start=0, end=len(text), text=text, kind=kind)
    units = [unit('a', '1.1 Classification', 1, 'HEADING'), unit('b', 'Classification conclusion', 2),
             unit('c', '1.2 Regression', 2, 'HEADING'), unit('d', 'Regression definition', 2)]
    assert unit_node_map(outline, units, reading_boundary=True) == {'a': 'first', 'b': 'first', 'c': 'second', 'd': 'second'}
    assert unit_node_map(outline, units)['b'] == 'second'  # Existing frozen planning semantics.


def test_repeated_parent_running_header_does_not_steal_convolution_body():
    outline = DocumentOutline(source_sha256='a' * 64, page_count=4, nodes=[
        OutlineNode(node_id='layers', parent_id=None, title='14.2 Common layers', kind='SECTION',
                    level=1, position=0, start=1, end=4, basis='PDF_TOC'),
        OutlineNode(node_id='conv', parent_id='layers', title='14.2.1 Convolutional layers', kind='SECTION',
                    level=2, position=1, start=1, end=3, basis='PDF_TOC'),
        OutlineNode(node_id='pool', parent_id='layers', title='14.2.2 Pooling layers', kind='SECTION',
                    level=2, position=2, start=4, end=4, basis='PDF_TOC')], segments=[], issues=[])
    def unit(key, text, page, kind='TEXT'):
        return Unit(unit_id=key, block_id=key, page=page, start=0, end=len(text), text=text, kind=kind)
    units = [unit('a', '14.2 Common layers', 1, 'HEADING'),
             unit('b', '14.2.1 Convolutional layers', 1, 'HEADING'),
             unit('c', 'Convolution definition', 1),
             unit('d', '14.2. Common layers', 2, 'HEADING'),
             unit('e', 'Convolution equation', 2),
             unit('f', '14.2.2 Pooling layers', 4, 'HEADING'),
             unit('g', 'Pooling definition', 4)]
    owners = unit_node_map(outline, units, reading_boundary=True)
    assert owners['d'] == owners['e'] == 'conv'
    assert owners['f'] == owners['g'] == 'pool'
    assert unit_node_map(outline, units)['e'] == 'layers'
