"""Initialize an empty, portable managed instance without importing private PDFs."""

import argparse
import importlib.util
from pathlib import Path


def initialize(run, library, *, port=8011, harness_port=3090, repo=None):
    repo = (repo or Path(__file__).resolve().parents[1]).resolve()
    run, library = run.resolve(), library.resolve()
    if not (repo / 'dist/index.js').is_file():
        raise ValueError('Build the plugin first: npm run build')
    if (run / 'manifest.json').exists() or run.exists() and any(run.iterdir()):
        raise ValueError('Instance directory is not empty; existing configuration is preserved')
    if port == harness_port or not all(1 <= p <= 65535 for p in (port, harness_port)):
        raise ValueError('Choose distinct valid API and DSH ports')
    spec = importlib.util.spec_from_file_location('managed_workspace', Path(__file__).with_name('formal-workspace.py'))
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    runtime.REPO = repo
    run.mkdir(parents=True, exist_ok=True)
    runtime.dump(run / 'manifest.json', {'schema_version': 'production-workspace-v1',
        'config_dir': str(repo), 'library': str(library), 'port': port, 'harness_port': harness_port,
        'url': f'http://127.0.0.1:{port}/app/', 'parse_cache_libraries': [],
        'vision_cache_runs': [], 'code': {}, 'documents': [], 'baseline_states': {}})
    runtime.dump(run / 'runtime-overrides.json', {'runtime_mode': 'production',
        'require_parse_reuse': False, 'reason': 'Normal PDF intake; fresh parsing is enabled'})
    runtime.freeze(run, reason='Initial portable production code snapshot')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8011)
    parser.add_argument('--harness-port', type=int, default=3090)
    args = parser.parse_args()
    initialize(args.run, args.library, port=args.port, harness_port=args.harness_port)
