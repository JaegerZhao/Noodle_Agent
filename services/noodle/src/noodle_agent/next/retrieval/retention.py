"""Explicit, backed-up reclamation of old disposable index projections."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ..contracts import canonical
from ..repository import Conflict, rows


def plan(store, *, keep=3, min_age_days=7, protected=(), at=None):
    if keep < 1 or min_age_days < 0:
        raise ValueError('Keep at least one generation and use a non-negative retention age')
    cutoff = (at or datetime.now(UTC)) - timedelta(days=min_age_days)
    with store.db.engine.connect() as c:
        heads = {r['generation'] for r in rows(c, 'SELECT generation FROM index_heads')}
        generations = rows(c, 'SELECT * FROM index_generations ORDER BY created_at DESC,id DESC')
        counts = {r['generation']: r['n'] for r in rows(c, 'SELECT generation,count(*) n FROM index_entries GROUP BY generation')}
    retained, retired, buckets = [], [], {}
    for value in generations:
        item = dict(value)
        key = (item['mode_key'], item['backend'])
        buckets[key] = buckets.get(key, 0) + 1
        age = datetime.fromisoformat(item['created_at'])
        if age.tzinfo is None:
            age = age.replace(tzinfo=UTC)
        reasons = [name for name, test in [('ACTIVE_HEAD', item['id'] in heads),
            ('PINNED', item['id'] in protected), ('RECENT_GENERATION', buckets[key] <= keep),
            ('RETENTION_WINDOW', age >= cutoff), ('EXTERNAL_BACKEND', item['backend'] != 'exact'),
            ('INCOMPLETE', item['status'] != 'READY')] if test]
        (retained if reasons else retired).append({'generation': item['id'], 'reasons': reasons,
                                                   'entries': counts.get(item['id'], 0)})
    return {'policy': 'index-retention-v1', 'heads': sorted(heads), 'retained': retained,
            'candidates': retired, 'candidate_count': len(retired), 'destructive': False}


def apply(store, expected, *, keep=3, min_age_days=7, protected=(), limit=5):
    """Opt-in only. Preserve a full recovery package and all shared content/assets."""
    from ..backup import recovery_zip

    current = plan(store, keep=keep, min_age_days=min_age_days, protected=protected)
    if current != expected or not 1 <= limit <= 32:
        raise Conflict('Retention inputs changed or batch size is invalid; preview again')
    identities = [v['generation'] for v in current['candidates'][:limit]]
    if not identities:
        return {'status': 'NO_CHANGE', 'removed': []}
    with store.db.engine.connect() as c:
        if c.exec_driver_sql("SELECT 1 FROM jobs WHERE status='RUNNING' LIMIT 1").first():
            raise Conflict('Pause active workers before index reclamation')
    backup = store.root.parent / (store.root.name + '-index-recovery') / (str(uuid4()) + '.zip')
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(recovery_zip(store))
    with store.repository.transaction() as c:
        heads = sorted(r['generation'] for r in rows(c, 'SELECT generation FROM index_heads'))
        if heads != current['heads'] or c.exec_driver_sql("SELECT 1 FROM jobs WHERE status='RUNNING' LIMIT 1").first():
            raise Conflict('Index activity changed during backup; nothing was removed')
        existing = {r['id'] for r in rows(c, 'SELECT id FROM index_generations')}
        if not set(identities) <= existing:
            raise Conflict('Another maintenance operation changed the generations')
        values = {'ids': canonical(identities)}
        from sqlalchemy import text
        c.execute(text('DELETE FROM knowledge_fts WHERE generation IN (SELECT value FROM json_each(:ids))'), values)
        c.execute(text('DELETE FROM index_entries WHERE generation IN (SELECT value FROM json_each(:ids))'), values)
        c.execute(text('DELETE FROM index_generations WHERE id IN (SELECT value FROM json_each(:ids))'), values)
    # Shared vectors, chunks, original evidence, revisions and frozen code are retained.
    return {'status': 'RECLAIMED', 'removed': identities, 'recovery_package': str(backup),
            'note': 'Restore the recovery package into a separate directory; database free pages are reused, no online VACUUM.'}
