"""Visual cost regressions use real checkpoints and a provider that cannot use a network."""

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from test_next_source_sections import Agent, Parser, prepared  # noqa: F401
from test_next_workflow_readiness import corpus  # noqa: F401

from noodle_agent.domain import VisualUnderstanding
from noodle_agent.next.checkpoints import Checkpoints
from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.generation import Generation
from noodle_agent.next.generation_contracts import Unit
from noodle_agent.next.page_evidence import PageEvidence
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.repository import InvalidKnowledge, Repository, storage_path_key
from noodle_agent.next.source_sections import units_from_blocks
from noodle_agent.next.visual_context import cached_page_visual, page_contexts
from noodle_agent.next.visual_policy import native_text, page_evidence_complete, prompt_hashes
from noodle_agent.next.visual_replay import VerifiedPageCache, verified_page_manifest


def unit(key='image', page=1, text='', asset='assets/image.png', visual=None):
    return Unit(unit_id=key, block_id=key, page=page, start=0, end=max(1, len(text)),
                text=text or '教学图片：' + str(asset), kind='IMAGE' if asset else 'TEXT',
                asset_ref=asset, visual=visual)


def generation_stub():
    return SimpleNamespace(store=None, journal=None, parsing=SimpleNamespace(vision=None),
        lease=SimpleNamespace(input={'revision_id': 'source'}), progress=lambda *_: None)


@pytest.mark.parametrize('visual,expected', [
    (None, {1}),
    ({'is_decorative': True}, set()),
    ({'is_decorative': True, 'essential': True}, {1}),
    ({'caption': 'A simple diagram', 'confidence': 1}, {1}),
    ({'asset_type': 'DIAGRAM', 'group_type': 'MULTI_PANEL', 'confidence': 1, 'ocr_text': ['x=1']}, {1}),
    ({'asset_type': 'FORMULA', 'confidence': 1, 'ocr_text': ['x=1']}, set()),
    ({'asset_type': 'FORMULA', 'confidence': 1, 'ocr_text': ['y=2']}, {1}),
    ({'asset_type': 'CODE', 'confidence': 1, 'ocr_text': ['x=1'], 'caption': '无法辨认下半部'}, {1}),
])
def test_page_routing_keeps_unknown_images_and_only_reuses_actual_extraction(visual, expected):
    units = [unit(text='x=1', visual=visual), unit('native', text='完整原文。' * 30 + ' x=1', asset=None)]
    with PageEvidence(generation_stub(), units) as evidence:
        assert evidence.needed == expected and not evidence.futures


@pytest.mark.parametrize('version,delta,native,confidence,expected', [
    ('source-page-evidence-v1', ['x=1'], '', .95, True),
    ('source-page-evidence-v2', ['x=1'], '', .95, True),
    ('source-page-evidence-v2', [], '完整来源文字。' * 30, .95, True),
    ('source-page-evidence-v2', [], '', .99, False),
    ('source-page-evidence-v2', [], '\ufffd' * 100, .99, False),
    ('source-page-evidence-v2', [], 'def broken(:\n' + 'a' * 100, .99, False),
    ('source-page-evidence-v2', ['无法辨认输出面板'], '来源文字。' * 30, .99, False),
    ('source-page-evidence-v2', ['x=1'], '', .7, False),
])
def test_page_coverage_is_not_just_confidence_or_nonempty_ocr(version, delta, native, confidence, expected):
    units = [unit('native', text=native, asset=None)] if native else []
    detail = VisualUnderstanding(external_block_id='page-1', image_sha256='image', model='fixture',
        prompt_version=version, ocr_text=delta, confidence=confidence).model_dump()
    context = {'page': 1, 'text': canonical(detail), 'input': {'native_sha256': sha(native_text(units)),
        'image_sha256': 'image', 'model': 'fixture'}}
    assert page_evidence_complete(context, units) is expected
    if expected:
        context['input']['image_sha256'] = 'another-image'
        assert not page_evidence_complete(context, units)


def test_known_visual_metadata_survives_unit_building_without_changing_source():
    block = {'id': 'image', 'page': 1, 'kind': 'IMAGE', 'text': '', 'asset_ref': 'assets/image.png',
             'visual': {'is_decorative': True}}
    units = units_from_blocks({'blocks': [block]})
    assert units[0].visual == block['visual'] and block['text'] == ''


class Vision:
    enabled = True
    model_name = 'fixture'

    def __init__(self):
        self.calls = []

    def analyze(self, path, *, external_block_id, context):
        self.calls.append(context)
        return VisualUnderstanding(external_block_id=external_block_id, image_sha256=sha(path.read_bytes()),
            model=self.model_name, prompt_version='source-page-evidence-v2', confidence=.95,
            ocr_text=['Term frequency counts occurrences.'])

    def analyze_batch(self, items):
        raise AssertionError('A complete v2 page must not buy another crop call')


def test_complete_v2_evidence_skips_crop_calls_through_actual_generation(prepared):  # noqa: F811
    store, lease, source = prepared
    image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), False).tobytes('png')
    asset = 'assets/' + sha(image) + '.png'
    store.put(asset, image)
    artifact = {'title': 'Example', 'page_count': 2, 'blocks': [
        {'id': 'h1', 'page': 1, 'kind': 'HEADING', 'text': 'Term frequency'},
        {'id': 'image', 'page': 1, 'kind': 'IMAGE', 'text': '', 'asset_ref': asset},
        {'id': 'h2', 'page': 2, 'kind': 'HEADING', 'text': 'Inverse document frequency'},
        {'id': 'b', 'page': 2, 'kind': 'TEXT', 'text': 'IDF depends on document frequency.'}]}
    profile = {'parser': Parser.name, 'generation_layout': 'source-sections-v1',
        'execution_policy': 'pipeline-throughput-v1', 'checking_policy': 'single-check-v1'}
    parse_id = store.sources.record_parse(source['revision_id'], 'raw', {}, artifact, lease=lease)
    with store.db.engine.connect() as c:
        digest = c.exec_driver_sql('SELECT sha256 FROM source_revisions WHERE id=?', (source['revision_id'],)).scalar_one()
    journal = Checkpoints(store.repository, lease)
    journal.save('parse.normalized', {'revision': source['revision_id'], 'sha256': digest, 'profile': profile},
                 {'parse_id': parse_id, 'artifact': artifact})
    vision = Vision()
    result = Generation(store, Parser(), Agent(), lease, profile=profile, vision=vision).run()
    assert len(result['notes']) == 2 and len(vision.calls) == 1
    assert vision.calls[0]['evidence_policy'] == 'native-delta-v1'
    assert len(Generation(store, Parser(), Agent(), lease, profile=profile, vision=vision).run()['notes']) == 2
    assert len(vision.calls) == 1


def second_journal(store, lease):
    store.jobs.enqueue(lease.input['event_id'], 'VISUAL_REUSE_TEST', lease.input)
    return Checkpoints(store.repository, store.jobs.claim('cache-test', kind='VISUAL_REUSE_TEST'))


def test_v2_cache_reuses_across_jobs_but_not_changed_native_or_image(corpus, tmp_path):  # noqa: F811
    store, _, lease, journal, _, _ = corpus
    path = tmp_path / 'page.png'
    path.write_bytes(b'page-image')
    context = {'page': 1, 'extraction_mode': 'SOURCE_PAGE', 'native_text': 'existing text', 'evidence_policy': 'native-delta-v1'}
    vision = Vision()
    first = cached_page_visual(store, journal, vision, 'source-hash', 1, path, context)
    other = second_journal(store, lease)
    assert cached_page_visual(store, other, vision, 'source-hash', 1, path, context) == first
    assert len(vision.calls) == 1
    cached_page_visual(store, other, vision, 'source-hash', 1, path, {**context, 'native_text': 'changed'})
    cached_page_visual(store, other, vision, 'another-source', 1, path, context)
    path.write_bytes(b'changed-image')
    cached_page_visual(store, other, vision, 'source-hash', 1, path, context)
    assert len(vision.calls) == 4


def test_page_contexts_pass_only_the_corresponding_pages_native_text(prepared):  # noqa: F811
    store, lease, source = prepared
    units = [unit('first', 1, 'native on page one', None), unit('second', 2, 'native on page two', None)]
    vision = Vision()
    contexts = page_contexts(store, Checkpoints(store.repository, lease), vision, source['revision_id'], units,
                             source_aligned=True, selected_pages=[1, 2])
    assert {c['page']: c['native_text'] for c in vision.calls} == {1: 'native on page one', 2: 'native on page two'}
    assert len(contexts) == 2


def test_v2_external_cache_is_bound_to_prompt_and_exact_native_context(tmp_path):
    path = tmp_path / 'page.png'
    path.write_bytes(b'image')
    context = {'page': 1, 'extraction_mode': 'SOURCE_PAGE', 'evidence_policy': 'native-delta-v1', 'native_text': 'native'}
    value = VisualUnderstanding(external_block_id='page-1', image_sha256=sha(path.read_bytes()), model='fixture',
                                prompt_version='source-page-evidence-v2', confidence=.95)
    hashes = prompt_hashes()
    record = {'page': 1, 'image_sha256': value.image_sha256,
        'input_sha256': VerifiedPageCache.input_hash('page-1', context), 'value': value.model_dump(),
        'prompt_sha256': hashes[value.prompt_version], 'origin_root': 'frozen', 'origin_ref': 'receipt',
        'historical_tokens': 100, 'historical_ms': 200}
    manifest = {'source_sha256': 'source', 'model': 'fixture', 'prompt_sha256s': hashes, 'records': [record]}
    provider, hits = Vision(), []
    cache = VerifiedPageCache(provider, manifest, source_sha256='source', emit=hits.append)
    assert cache.analyze(path, external_block_id='page-1', context=context) == value
    assert len(hits) == 1 and not provider.calls
    cache.analyze(path, external_block_id='page-1', context={**context, 'native_text': 'changed'})
    assert len(provider.calls) == 1
    manifest['prompt_sha256s'] = {**hashes, 'source-page-evidence-v2': 'wrong-prompt'}
    with pytest.raises(QualityFailure, match='source/model/prompt'):
        VerifiedPageCache(provider, manifest, source_sha256='source')


def test_manifest_loads_verified_v2_checkpoint_and_successful_receipt(tmp_path):
    root = tmp_path / 'run'
    library = root / 'library'
    library.mkdir(parents=True)
    source = b'%PDF-fixture'
    image = b'image'
    (library / 'source.pdf').write_bytes(source)
    (library / 'page.png').write_bytes(image)
    version = 'source-page-evidence-v2'
    from importlib.resources import files
    prompt = files('noodle_agent').joinpath('prompts', 'source_page_evidence.v2.md').read_text(encoding='utf-8')
    dest = root / 'execution-code/services/noodle/src/noodle_agent/prompts/source_page_evidence.v2.md'
    dest.parent.mkdir(parents=True)
    dest.write_text(prompt, encoding='utf-8')
    settings = SimpleNamespace(vision_model='fixture', vision_base_url='fixture', vision_reasoning_effort='low', vision_max_output_tokens=4800)
    (root / 'run-input.json').write_text(json.dumps({'source_sha256': sha(source), 'source': {'revision_id': 'source'}}))
    (root / 'runtime-config-redacted.json').write_text(json.dumps(vars(settings)))
    value = VisualUnderstanding(external_block_id='page-1', image_sha256=sha(image), model='fixture', prompt_version=version)
    context = {'page': 1, 'extraction_mode': 'SOURCE_PAGE', 'evidence_policy': 'native-delta-v1', 'native_text': 'native'}
    receipt = {'status': 'COMPLETED', 'model': 'fixture', 'prompt_version': version, 'image_sha256': sha(sha(image)),
        'input_sha256': VerifiedPageCache.input_hash('page-1', context), 'total_tokens': 100, 'duration_ms': 200}
    checkpoint = {'page': 1, 'asset_ref': 'page.png', 'text': value.model_dump_json()}
    records = [('receipt.json', receipt), ('checkpoint.json', checkpoint)]
    for name, data in records:
        (library / name).write_text(canonical(data), encoding='utf-8')
    with sqlite3.connect(library / 'noodle.db') as c:
        c.executescript('CREATE TABLE source_revisions(id,sha256,blob_ref); CREATE TABLE build_artifacts(job_id,storage_ref,sha256,kind); '
                       'CREATE TABLE job_checkpoints(job_id,artifact_ref,artifact_sha256,stage,state);')
        c.execute('INSERT INTO source_revisions VALUES(?,?,?)', ('source', sha(source), 'source.pdf'))
        c.execute('INSERT INTO build_artifacts VALUES(?,?,?,?)', ('job', 'receipt.json', sha(canonical(receipt)), 'VISION_CALL'))
        c.execute('INSERT INTO job_checkpoints VALUES(?,?,?,?,?)', ('job', 'checkpoint.json', sha(canonical(checkpoint)), 'page-context.source-v4.1.hash', 'DONE'))
    manifest = verified_page_manifest([root], sha(source), settings)
    assert not manifest['rejected_roots'] and len(manifest['records']) == 1
    provider = Vision()
    cache = VerifiedPageCache(provider, manifest, source_sha256=sha(source))
    assert cache.analyze(library / 'page.png', external_block_id='page-1', context=context) == value
    assert not provider.calls


@pytest.mark.skipif(os.name != 'nt', reason='Windows extended path aliases')
@pytest.mark.parametrize('root_extended,result_extended', [(False, True), (True, False), (True, True)])
def test_windows_extended_path_alias_does_not_bypass_storage_boundary(tmp_path, monkeypatch, root_extended, result_extended):
    root = tmp_path.resolve()
    repository = object.__new__(Repository)
    repository.root = Path('\\\\?\\' + str(root)) if root_extended else root
    def path(value):
        return Path('\\\\?\\' + str(value)) if result_extended else value
    inside = path(root / 'assets/image.png')
    monkeypatch.setattr(Path, 'resolve', lambda _self: inside)
    assert repository.path('assets/image.png') == inside
    # Resolving a symlink/junction to a sibling still counts as an escape.
    outside = path(root.parent / (root.name + '-sibling') / 'image.png')
    monkeypatch.setattr(Path, 'resolve', lambda _self: outside)
    with pytest.raises(InvalidKnowledge, match='escapes root'):
        repository.path('assets/image.png')
    monkeypatch.setattr(Path, 'resolve', lambda _self: inside)
    with pytest.raises(InvalidKnowledge, match='escapes root'):
        repository.path(str(inside))


@pytest.mark.skipif(os.name != 'nt', reason='Windows UNC path aliases')
def test_windows_unc_extended_prefix_has_the_same_storage_key():
    assert storage_path_key(Path(r'\\?\UNC\server\share\folder')) == Path(r'\\server\share\folder')
