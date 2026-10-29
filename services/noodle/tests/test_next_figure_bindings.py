"""Figure bindings use source captions, not legal-path coincidence or model review."""

from noodle_agent.next.figure_bindings import bind_markdown, catalogue


def block(key, page, caption, panel='', x=0):
    return {'id': key, 'page': page, 'asset_ref': f'assets/{key}.jpg', 'text': '',
            'bbox': [x, 20, x + 100, 100],
            'metadata': {'mineru': {'chart_caption': [panel, caption]}}}


def test_multipart_chart_recovers_caption_on_last_panel_and_resolves_id():
    figures = catalogue({'blocks': [block('a', 91, '', '(a)'), block('b', 91, 'Figure 2.15: pdf and log-pdf', '(b)', 120)]})
    assert len(figures) == 1
    assert [p['block_id'] for p in figures[0]['panels']] == ['a', 'b']
    changes = []
    result = bind_markdown(f"![图2.15对比](figure:{figures[0]['figure_id']})", figures, diagnostics=changes)
    assert 'a.jpg' in result and 'b.jpg' in result and 'figure:' not in result
    assert len(changes) == 1


def test_repeated_wrong_legal_asset_is_replaced_by_number_and_unnumbered_evidence():
    figures = catalogue({'blocks': [block('sprinkler', 129, 'Figure 3.14: Water sprinkler'), block('markov', 131, 'Figure 3.15: Markov models')]})
    wrong = '![洒水车网络](../assets/markov.jpg)'
    assert 'sprinkler.jpg' in bind_markdown(wrong, figures, evidence_text='See Figure 3.14.', evidence_pages={129, 130})
    correct = '![马尔可夫图](../assets/markov.jpg)'
    assert bind_markdown(correct, figures, evidence_text='See Figure 3.15.', evidence_pages={131, 132}) == correct


def test_ambiguous_unnumbered_images_are_not_semantically_guessed():
    figures = catalogue({'blocks': [block('a', 1, 'Figure 1.1: first'), block('b', 2, 'Figure 1.2: second')]})
    text = '![模型图](../assets/b.jpg)'
    assert bind_markdown(text, figures, evidence_text='Figure 1.1 and Figure 1.2', evidence_pages={1}) == text
    assert bind_markdown(text, figures, evidence_text='No figure identity', evidence_pages={1}) == text


def test_separate_pages_and_unlabelled_images_do_not_form_a_group():
    figures = catalogue({'blocks': [block('a', 1, '', '(a)'), block('b', 2, 'Figure 1.2: second', '(b)')]})
    assert [p['block_id'] for p in figures[0]['panels']] == ['b']


def test_explicit_panel_selects_only_that_panel():
    figures = catalogue({'blocks': [block('a', 1, '', '(a)'), block('b', 1, 'Figure 1.2: two plots', '(b)')]})
    assert bind_markdown(f"![图1.2a](figure:{figures[0]['figure_id']})", figures) == '![图1.2a](../assets/a.jpg)'


def test_conflicting_alt_number_and_uncatalogued_crop_are_not_replaced():
    figures = catalogue({'blocks': [block('a', 1, 'Figure 1.1: first'), block('b', 2, 'Figure 1.2: second')]})
    text = '![图1.1](../assets/b.jpg)'
    assert bind_markdown(text, figures, evidence_text='See Figure 1.2.', evidence_pages={2}) == text
    unknown = '![图1.1](../assets/unknown-panel.jpg)'
    assert bind_markdown(unknown, figures, evidence_text='Figure 1.1', evidence_pages={1}) == unknown
