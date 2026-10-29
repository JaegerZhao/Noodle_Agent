"""Offline baseline and isolated preparation for incremental Concept maintenance."""

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIRECTORIES = ('revisions', 'artifacts', 'assets', 'blobs', 'vectors', 'indexes')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_db(root):
    connection = sqlite3.connect((root / 'noodle.db').as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def inspect(root):
    with closing(read_db(root)) as c:
        c.execute('BEGIN')
        result = {}
        queries = {
            'heads': 'SELECT n.id,n.kind,n.title,n.visibility,n.current_revision_id,r.markdown_ref,r.markdown_sha256 FROM notes n LEFT JOIN note_revisions r ON r.id=n.current_revision_id ORDER BY n.id',
            'sources': 'SELECT id,current_revision_id,deleted_at FROM sources ORDER BY id',
            'storage': 'SELECT * FROM storage_contract',
            'indexes': 'SELECT * FROM index_heads ORDER BY mode_key,backend',
            'claims': 'SELECT * FROM concept_identity_claims ORDER BY concept_id,identity_key',
            'routes': 'SELECT * FROM atomic_entity_routes ORDER BY atomic_revision_id',
            'contributions': 'SELECT * FROM contribution_decisions ORDER BY id',
            'links': 'SELECT l.* FROM links l JOIN notes n ON n.current_revision_id=l.from_revision_id ORDER BY l.id',
            'active_jobs': "SELECT id,kind,status FROM jobs WHERE status IN ('PENDING','RUNNING','RETRY') ORDER BY id",
            'provider_baseline': 'SELECT kind,count(*) calls,coalesce(sum(input_tokens),0) input_tokens,coalesce(sum(output_tokens),0) output_tokens FROM provider_calls GROUP BY kind ORDER BY kind',
        }
        for key, sql in queries.items():
            values = [dict(r) for r in c.execute(sql)]
            result[key] = values if key not in {'routes', 'contributions', 'claims', 'links'} else {'count': len(values), 'sha256': signature(values)}
        result['provider_rowid'] = c.execute('SELECT coalesce(max(rowid),0) FROM provider_calls').fetchone()[0]
        result['job_rowid'] = c.execute('SELECT coalesce(max(rowid),0) FROM jobs').fetchone()[0]
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='relation_overrides'").fetchone():
            overrides = [dict(r) for r in c.execute('SELECT * FROM relation_overrides ORDER BY owner_note_id,relation_key')]
            if overrides:
                result['relation_overrides'] = {'count': len(overrides), 'sha256': signature(overrides)}
    for note in result['heads']:
        if note['markdown_ref']:
            path = (root / note['markdown_ref']).resolve()
            if not path.is_relative_to(root) or digest(path) != note['markdown_sha256']:
                raise ValueError('Head content verification failed: ' + note['id'])
    return result


def dump(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def prepare(base, run, *, resume_pending=False):
    if run == base or run.is_relative_to(base) or base.is_relative_to(run):
        raise ValueError('Preparation requires a separate run directory')
    if not run.is_relative_to((REPO / '.local-only').resolve()):
        raise ValueError('Maintenance runs must be inside this workspace .local-only')
    baseline = inspect(base)
    if any(j['status'] == 'RUNNING' for j in baseline['active_jobs']) or baseline['active_jobs'] and not resume_pending:
        raise ValueError('Wait for active producers before taking a maintenance baseline')
    manifest_path = run / 'baseline.json'
    if run.exists():
        if not manifest_path.exists():
            raise ValueError('Existing destination is not a prepared maintenance run')
        saved = json.loads(manifest_path.read_text(encoding='utf-8'))
        if saved['base'] != str(base) or saved['baseline_sha256'] != signature(baseline):
            raise ValueError('Baseline changed; do not resume into a different input')
        if (run / 'prepared.json').exists():
            return verify(base, run)
    else:
        run.mkdir(parents=True)
        dump(manifest_path, {'base': str(base), 'baseline': baseline,
            'baseline_sha256': signature(baseline), 'created_at': datetime.now(UTC).isoformat(),
            'allowed_phases': ['inspect', 'prepare', 'verify'], 'provider_calls_allowed': False})
    library = run / 'library'
    library.mkdir(exist_ok=True)
    files = [f for d in DIRECTORIES for f in (base / d).rglob('*') if f.is_file() and not f.name.endswith('.tmp')]
    required = sum(f.stat().st_size for f in files if not (library / f.relative_to(base)).exists())
    if shutil.disk_usage(run).free < required:
        raise ValueError('Insufficient disk space for independent copy')
    with closing(read_db(base)) as source, closing(sqlite3.connect(library / 'noodle.db')) as destination:
        source.backup(destination)
        if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite backup failed integrity check')
    receipts = []
    for source in files:
        if source.is_symlink() or not source.resolve().is_relative_to(base):
            raise ValueError('Source file escapes library')
        relative = source.relative_to(base)
        target = library / relative
        expected = digest(source)
        if not target.exists() or digest(target) != expected:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if digest(target) != expected:
            raise ValueError('Copy hash mismatch: ' + str(relative))
        receipts.append({'path': relative.as_posix(), 'sha256': expected, 'bytes': source.stat().st_size})
    code = []
    for source in [*sorted((REPO / 'services/noodle/src').rglob('*')), Path(__file__)]:
        if not source.is_file() or '__pycache__' in source.parts:
            continue
        relative = source.relative_to(REPO)
        target = run / 'execution-code' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        code.append({'path': relative.as_posix(), 'sha256': digest(source)})
    if signature(inspect(base)) != signature(baseline) or inspect(library) != baseline:
        raise ValueError('Knowledge changed during snapshot preparation')
    dump(run / 'file-manifest.json', receipts)
    dump(run / 'execution-code.json', code)
    result = {'status': 'PREPARED', 'files': len(receipts), 'bytes': sum(f['bytes'] for f in receipts),
        'library': str(library), 'provider_calls': 0, 'baseline_sha256': signature(baseline),
        'historical_jobs_not_scheduled': True}
    dump(run / 'prepared.json', result)
    return result


def verify(base, run):
    saved = json.loads((run / 'baseline.json').read_text(encoding='utf-8'))
    if saved['base'] != str(base):
        raise ValueError('Wrong baseline library')
    baseline_unchanged = signature(inspect(base)) == saved['baseline_sha256']
    clone = inspect(run / 'library')
    expected = [r for r in saved['baseline']['heads'] if r['kind'] == 'ATOMIC']
    actual = [r for r in clone['heads'] if r['kind'] == 'ATOMIC']
    return {'status': 'VERIFIED' if baseline_unchanged and actual == expected else 'CHANGED',
        'baseline_unchanged': baseline_unchanged, 'atomic_heads_unchanged': actual == expected,
        'provider_rowid_delta': clone['provider_rowid'] - saved['baseline']['provider_rowid']}


def freeze_code(run):
    active = run / 'execution-context.json'
    budget = run / 'call-budget.json'
    if active.exists() and budget.exists() and json.loads(budget.read_text(encoding='utf-8')).get('calls'):
        previous = json.loads(active.read_text(encoding='utf-8'))
        destination = Path(previous['path'])
        for item in previous['files']:
            if digest(destination / item['path']) != item['sha256']:
                raise ValueError('Frozen execution code changed during a recorded run')
        return destination
    files = [p for p in sorted((REPO / 'services/noodle/src').rglob('*'))
             if p.is_file() and '__pycache__' not in p.parts]
    manifest = [{'path': p.relative_to(REPO).as_posix(), 'sha256': digest(p)} for p in files]
    code_sha = signature(manifest)
    destination = run / ('execution-code-' + code_sha[:16])
    for source, item in zip(files, manifest, strict=True):
        target = destination / item['path']
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if digest(target) != item['sha256']:
            raise ValueError('Frozen execution code hash differs')
    receipt = {'code_sha256': code_sha, 'path': str(destination), 'files': manifest}
    if active.exists() and json.loads(active.read_text(encoding='utf-8'))['code_sha256'] != code_sha:
        budget = run / 'call-budget.json'
        if budget.exists() and json.loads(budget.read_text(encoding='utf-8')).get('calls'):
            raise ValueError('Do not change execution code during the recorded model run')
    dump(active, receipt)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['inspect', 'prepare', 'verify', 'plan', 'apply', 'report', 'audit', 'index', 'format', 'repair'])
    parser.add_argument('library', type=Path)
    parser.add_argument('--run', type=Path)
    parser.add_argument('--allow-models', action='store_true')
    parser.add_argument('--allow-embeddings', action='store_true')
    parser.add_argument('--embedding-budget', type=int)
    parser.add_argument('--findings-file', type=Path)
    parser.add_argument('--atomic-id', action='append', help='Scope a new recovery plan to these existing Atomic IDs')
    parser.add_argument('--resume-pending', action='store_true', help='Copy a stopped checkpoint including pending work; running leases are forbidden')
    args = parser.parse_args()
    base = args.library.resolve()
    if args.phase != 'inspect' and args.run is None:
        parser.error('--run is required')
    if args.phase in {'plan', 'apply', 'report', 'audit', 'index', 'format', 'repair'}:
        run = args.run.resolve()
        if not run.is_relative_to((REPO / '.local-only').resolve()) or run == base or base.is_relative_to(run):
            raise ValueError('Maintenance execution requires its independent prepared run')
        saved = json.loads((run / 'baseline.json').read_text(encoding='utf-8'))
        if saved['base'] != str(base) or signature(inspect(base)) != saved['baseline_sha256']:
            raise ValueError('Formal baseline changed since preparation')
        code = freeze_code(run) if args.phase == 'apply' else REPO
        sys.path.insert(0, str(code / 'services/noodle/src'))
        from noodle_agent.next.concepts import maintenance_run
        from noodle_agent.next.store import Store
        store = Store(run / 'library')
        try:
            frozen = maintenance_run.plan(store, run, saved['baseline_sha256'], atomic_ids=args.atomic_id)
            if args.phase == 'plan':
                result = {'status': 'PLANNED', 'atomic_count': len(frozen['atomics']), 'batches': len(frozen['batches']), 'target_concepts': len(frozen['target_concepts']), 'budget': frozen['budget']}
            elif args.phase == 'apply':
                applied = maintenance_run.apply(store, run, frozen, allow_models=args.allow_models)
                result = {'status': applied['status'], 'steps': len(applied['outcomes']),
                          'elapsed_seconds': applied.get('elapsed_seconds'), 'model_calls': applied.get('model_budget', {}).get('calls')}
            elif args.phase == 'report':
                result = maintenance_run.report(store, run, frozen)
            elif args.phase == 'audit':
                from noodle_agent.next.concepts.maintenance_acceptance import audit
                result = audit(store, run)
                result = {k: v for k, v in result.items() if k not in {'concepts', 'roots'}}
            elif args.phase == 'format':
                from noodle_agent.next.concepts.format_maintenance import (
                    apply as format_concepts,
                )
                result = format_concepts(store, run)
                result = {**result, 'changed': len(result['changed'])}
            elif args.phase == 'repair':
                if not args.allow_models or not args.findings_file:
                    raise ValueError('Repair requires --allow-models and a located --findings-file')
                from noodle_agent.next.concepts.maintenance_acceptance import repair
                requests = json.loads(args.findings_file.read_text(encoding='utf-8'))
                result = repair(store, run, requests)
            else:
                from noodle_agent.next.concepts.maintenance_acceptance import index
                result = index(store, run, allow_embeddings=args.allow_embeddings, embedding_budget=args.embedding_budget)
        finally:
            store.close()
    else:
        result = inspect(base) if args.phase == 'inspect' else prepare(base, args.run.resolve(), resume_pending=args.resume_pending) if args.phase == 'prepare' else globals()[args.phase](base, args.run.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
