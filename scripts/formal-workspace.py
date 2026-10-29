"""Freeze and serve a managed workspace. Initialize with production-instance.py."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def baseline(library):
    with sqlite3.connect((library.resolve() / 'noodle.db').as_uri() + '?mode=ro', uri=True) as c:
        state = {t: c.execute(q).fetchall() for t, q in {
            'jobs': 'SELECT id,status,attempts FROM jobs ORDER BY id',
            'notes': 'SELECT id,current_revision_id FROM notes ORDER BY id',
            'controls': 'SELECT * FROM job_controls ORDER BY job_id',
            'provider_count': 'SELECT count(*) FROM provider_calls'}.items()}
    return digest(json.dumps(state, sort_keys=True).encode())


def environment(manifest, run):
    code_root = execution(run)['code_root']
    env = os.environ.copy()
    env['NOODLE_CONFIG_DIR'] = manifest.get('config_dir') or str(run.resolve().parents[1])
    env['NOODLE_CODE_GENERATION'] = str(execution(run)['generation'])
    env.update({'PYTHONIOENCODING': 'utf-8', 'PYTHONPATH': str(code_root / 'services/noodle/src'),
        'NOODLE_NEXT_DATA_DIR': manifest['library'], 'NOODLE_DATA_DIR': manifest['library'], 'NOODLE_DATABASE_URL': 'sqlite:///' + manifest['library'] + '/noodle.db',
        'NOODLE_APP_MODE': 'next', 'NOODLE_API_PREFIX': '/internal/next', 'NOODLE_API_BASE_URL': f"http://127.0.0.1:{manifest['port']}",
        'NOODLE_NEXT_PARSE_CACHE_LIBRARIES': json.dumps(manifest['parse_cache_libraries']), 'NOODLE_NEXT_VISION_CACHE_RUNS': json.dumps(manifest['vision_cache_runs']),
        'NOODLE_NEXT_VECTOR_BACKEND': 'exact', 'NOODLE_NEXT_CONCEPT_WORKERS': '2',
        'NOODLE_NEXT_COST_POLICY': os.getenv('NOODLE_NEXT_COST_POLICY', 'balanced-cost-v1'),
        'NOODLE_NEXT_PRO_USAGE': os.getenv('NOODLE_NEXT_PRO_USAGE', 'planning-pro')})
    env.update({'NOODLE_RUNTIME_DIR': str(run / 'runtime'), 'NOODLE_DSH_PROFILE': 'knowledge', 'NOODLE_PLUGIN_MODULE': str(code_root / 'dist/index.js'),
        'NOODLE_HARNESS_WEB_URL': f"http://127.0.0.1:{manifest['harness_port']}", 'DSH_WEB_PORT': str(manifest['harness_port'])})
    if manifest.get('reuse_policy') == 'MINERU_ONLY_NEW_VISION_AND_GENERATION':
        env['NOODLE_NEXT_REQUIRE_PARSE_REUSE'] = '1'
        env['NOODLE_NEXT_VISION_CACHE_RUNS'] = '[]'
    env['NOODLE_RUNTIME_MODE'] = 'validation'
    overrides = run / 'runtime-overrides.json'
    if overrides.is_file():
        value = json.loads(overrides.read_text(encoding='utf-8'))
        if set(value) - {'runtime_mode', 'require_parse_reuse', 'vision_cache_runs', 'reason', 'recorded_at'}:
            raise ValueError('Unknown formal runtime override')
        mode = value.get('runtime_mode', 'validation')
        if mode not in {'validation', 'production'}:
            raise ValueError('runtime_mode must be validation or production')
        env['NOODLE_RUNTIME_MODE'] = mode
        if 'require_parse_reuse' in value:
            env['NOODLE_NEXT_REQUIRE_PARSE_REUSE'] = '1' if value['require_parse_reuse'] else '0'
        if 'vision_cache_runs' in value:
            env['NOODLE_NEXT_VISION_CACHE_RUNS'] = json.dumps(value['vision_cache_runs'])
    if env['NOODLE_RUNTIME_MODE'] == 'production':
        # Production prefers exact caches but must permit new PDF intake.
        env['NOODLE_NEXT_REQUIRE_PARSE_REUSE'] = '0'
    return env


def execution(run):
    releases = sorted(run.glob('execution-generation-*.json'))
    if releases:
        value = json.loads(releases[-1].read_text(encoding='utf-8'))
        return {**value, 'code_root': Path(value['code_root'])}
    manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
    return {'generation':0 if manifest.get('schema_version') == 'production-workspace-v1' else 1,
            'code_root':run / 'code', 'code':manifest['code']}


def freeze(run, reason=None):
    """Record a new immutable code generation; never overwrite prior run code."""
    current = execution(run)
    number = current['generation'] + 1
    root = run / f'code-generation-{number:03d}'
    root.mkdir(exist_ok=False)
    files = {}
    for folder in ['services/noodle/src','dist','src','scripts','config/agent-presets/noodle-knowledge']:
        for path in (REPO / folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                rel = path.relative_to(REPO)
                target = root / rel
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,target)
                files[rel.as_posix()] = digest(path.read_bytes())
    # Client-module discovery must resolve this generation's package metadata,
    # not the mutable package.json several parent directories above it.
    shutil.copy2(REPO / 'package.json', root / 'package.json')
    files['package.json'] = digest((root / 'package.json').read_bytes())
    value = {'generation':number,'code_root':str(root),'code':files,'recorded_at':datetime.now(UTC).isoformat(),
        'reason':reason or 'Versioned code release; see docs/DEVELOPMENT-LOG.md. Prior code and model responses remain immutable.'}
    dump(run / f'execution-generation-{number:03d}.json',value)
    print(json.dumps({k:v for k,v in value.items() if k!='code'}),flush=True)


def start(run, role):
    manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
    env = environment(manifest, run)
    output = (run / (role + '.log')).open('ab', buffering=0)
    # Isolate long-running services from a launching terminal's Ctrl+C and stdin.
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
    release = execution(run)
    command = [sys.executable, '-u', str(release['code_root'] / 'scripts/formal-workspace.py'),
               'supervise' if role == 'api' else 'serve', str(run), '--role', role]
    if role == 'harness':
        node = os.getenv('NOODLE_NODE') or shutil.which('node')
        if not node:
            raise ValueError('Node.js is required; add node to PATH or set NOODLE_NODE')
        command = [node, '--env-file-if-exists=.env', str(REPO / 'scripts/start-harness.mjs')]
    process = subprocess.Popen(command, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                               stdout=output, stderr=subprocess.STDOUT, creationflags=flags,close_fds=True)
    receipt = {'pid': process.pid, 'role': role, 'started_at': datetime.now(UTC).isoformat(), 'code_generation':release['generation'],
               'cost_policy':env.get('NOODLE_NEXT_COST_POLICY'),
               'pro_usage':env.get('NOODLE_NEXT_PRO_USAGE'),
               'runtime_mode':env.get('NOODLE_RUNTIME_MODE'),
               'require_parse_reuse':env.get('NOODLE_NEXT_REQUIRE_PARSE_REUSE') == '1',
               'runtime_overrides_sha256':digest((run / 'runtime-overrides.json').read_bytes()) if (run / 'runtime-overrides.json').is_file() else None}
    dump(run / (role + '-process.json'), receipt)
    dump(run / f'{role}-process-{process.pid}.json', receipt)
    print(json.dumps({'role': role, 'pid': process.pid, 'log': str(run / (role + '.log'))}), flush=True)


def supervise(run, role):
    """Recover the read/API process only; never duplicate generation workers."""
    import time
    if role != 'api':
        raise ValueError('Only the API role supports bounded process recovery')
    for attempt in range(3):
        process = subprocess.Popen([sys.executable, '-X', 'faulthandler', '-u', str(Path(__file__).resolve()),
            'serve', str(run), '--role', role], cwd=REPO, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        dump(run / 'api-child-process.json', {'pid':process.pid,'attempt':attempt+1,'started_at':datetime.now(UTC).isoformat()})
        code = process.wait()
        print(json.dumps({'role':role,'event':'api-process-exit','exit_code':code,'attempt':attempt+1}),flush=True)
        if code == 0:
            return
        if attempt < 2:
            time.sleep(1)
    raise SystemExit('API stopped after three failed process attempts; inspect api.log')


def serve(run, role):
    manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
    os.environ.update(environment(manifest, run))
    sys.path.insert(0, str(execution(run)['code_root'] / 'services/noodle/src'))
    from noodle_agent.next.store import Store
    if role == 'api':
        import uvicorn
        from noodle_agent.next.api import create_app
        uvicorn.run(create_app(Path(manifest['library'])), host='127.0.0.1', port=manifest['port'])
    else:
        from noodle_agent.next.workspace_worker import run_workspace
        store = Store(Path(manifest['library']))
        run_workspace(store, emit=lambda v: print(json.dumps(v, ensure_ascii=False, default=str), flush=True))


def report(run):
    manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
    sys.path.insert(0, str(execution(run)['code_root'] / 'services/noodle/src'))
    from noodle_agent.next.convergence import convergence_report
    from noodle_agent.next.knowledge_roots import roots
    from noodle_agent.next.pipeline_state import readiness
    from noodle_agent.next.repository import rows
    from noodle_agent.next.store import Store
    from noodle_agent.next.workspace_worker import record_acceptance
    store = Store(Path(manifest['library']))
    try:
        with store.db.engine.connect() as c:
            sources = rows(c, "SELECT s.*,r.sha256 FROM sources s JOIN source_revisions r ON r.id=s.current_revision_id WHERE s.kind='PDF' ORDER BY s.created_at")
            jobs = [dict(r) for r in rows(c, 'SELECT id,kind,status,error_json,result_json FROM jobs ORDER BY created_at')]
            usage = [dict(r) for r in rows(c, 'SELECT kind,operation,model,status,count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(total_tokens) total_tokens,sum(elapsed_ms) service_ms FROM provider_calls GROUP BY kind,operation,model,status')]
        states = [readiness(store, s['current_revision_id']) for s in sources]
        convergence = convergence_report(store)
        readbacks = [record_acceptance(store,{'job_id':j['id']}) for j in jobs if j['kind']=='GENERATE_ATOMICS' and j['status']=='SUCCEEDED']
        changed = [name for name, value in manifest['code'].items() if digest((run / 'code' / name).read_bytes()) != value]
        release_checks = [{'generation':value['generation'],'unchanged':all(digest((Path(value['code_root']) / name).read_bytes()) == expected for name,expected in value['code'].items())} for value in [json.loads(p.read_text(encoding='utf-8')) for p in sorted(run.glob('execution-generation-*.json'))]]
        baseline_equal = all(baseline(Path(lib)) == expected for lib, expected in manifest['baseline_states'].items())
        result = {'recorded_at': datetime.now(UTC).isoformat(), 'source_count': len(sources), 'sources': [dict(v) for v in sources], 'readiness': states,
            'jobs': jobs, 'usage': usage, 'business_convergence':convergence, 'engineering_readbacks':readbacks, 'knowledge_roots': roots(store), 'frozen_code_unchanged': not changed, 'execution_generations':release_checks, 'baseline_states_unchanged': baseline_equal,
            'complete': len(states) == len(readbacks) == 3 and convergence['passed'] and convergence['knowledge_structure']['status']=='COMPLETE' and all(v['status']=='ENGINEERING_READBACK_PASSED' for v in readbacks) and all(v['notes_status'] == 'READY' and v['concept_maintenance']['status'] == 'COMPLETE' for v in states)}
        dump(run / 'result.json', result)
        print(json.dumps({k: v for k, v in result.items() if k not in {'sources', 'jobs', 'usage', 'knowledge_roots'}}, ensure_ascii=False), flush=True)
    finally:
        store.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['freeze', 'start', 'serve', 'supervise', 'report'])
    p.add_argument('run', nargs='?', type=Path)
    p.add_argument('--role', choices=['api', 'worker', 'harness'], default='api')
    p.add_argument('--reason', help='Audit reason for a new immutable code generation')
    args = p.parse_args()
    if args.run is None:
        p.error('A prepared run directory is required')
    elif args.command == 'freeze':
        freeze(args.run.resolve(),args.reason)
    else:
        globals()[args.command](args.run.resolve(), args.role) if args.command in {'start', 'serve', 'supervise'} else report(args.run.resolve())


if __name__ == '__main__':
    main()
