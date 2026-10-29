"""Portable launch initialization and generic PDF observer contracts."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_empty_instance_initializes_without_private_course_inputs(tmp_path):
    repo, run = tmp_path / 'repo', tmp_path / 'external-instance'
    (repo / 'dist').mkdir(parents=True)
    (repo / 'dist/index.js').write_text('export {}', encoding='utf-8')
    (repo / 'package.json').write_text('{}', encoding='utf-8')
    initialize = load('production-instance').initialize
    initialize(run, tmp_path / 'library', repo=repo, port=8881, harness_port=8882)
    manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
    runtime = load('formal-workspace')
    assert runtime.execution(run)['generation'] == 1
    env = runtime.environment(manifest, run)
    assert env['NOODLE_CONFIG_DIR'] == str(repo.resolve())
    assert env['NOODLE_RUNTIME_MODE'] == 'production'
    assert env['NOODLE_NEXT_REQUIRE_PARSE_REUSE'] == '0'
    assert manifest['documents'] == []
    with pytest.raises(ValueError, match='not empty'):
        initialize(run, tmp_path / 'library', repo=repo)


def test_pdf_observer_uses_real_timeline_and_stops_on_blocked_status():
    observer = load('validate-pdf-pipeline')
    writes, samples = [], []
    def handler(request):
        assert request.method == 'GET'
        writes.append(request.url.path)
        if request.url.path.endswith('/generation-tasks'):
            return httpx.Response(200, json={'items': [{'id': 'job', 'status': 'REVIEW',
                'created_at': 'now', 'source_revision_id': 'revision'}]})
        return httpx.Response(200, json={'job': {'status': 'REVIEW'}, 'elapsed_seconds': 42})
    with httpx.Client(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
        result = observer.observe(client, 'revision', pause=lambda _: pytest.fail('Must stop'), record=samples.append)
    assert result['elapsed_seconds'] == 42 and len(samples) == 1
    assert len(writes) == 2
