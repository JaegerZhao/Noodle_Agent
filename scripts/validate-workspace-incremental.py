"""Existing-core media acceptance on an explicitly prepared isolated library."""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/noodle/src'))

from noodle_agent.config import Settings
from noodle_agent.next.concepts.coordinator import request_media
from noodle_agent.next.concepts.entity_writing import IMAGE
from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.provider_limits import call_guard
from noodle_agent.next.repository import Conflict, one, rows
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store


def validate(run, title):
    if not run.is_relative_to(ROOT / '.local-only') or not (run / 'prepared.json').exists():
        raise ValueError('Use a prepared independent library')
    store = Store(run / 'library')
    started = time.monotonic()
    try:
        with store.db.engine.connect() as c:
            note = one(c, "SELECT id FROM notes WHERE kind='CONCEPT' AND visibility='ACTIVE' AND title=:t", t=title)
            baseline = c.exec_driver_sql('SELECT coalesce(max(rowid),0) FROM provider_calls').scalar_one()
            atomic_hashes = [tuple(r) for r in c.exec_driver_sql("SELECT n.id,n.current_revision_id,r.markdown_sha256 FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='ATOMIC' ORDER BY n.id")]
        if not note:
            raise ValueError('Existing target not found')
        old = store.get_note(note['id'])
        request = request_media(store, note['id'], old['revision']['id'])
        def scope(value):
            if value['operation'] != 'next.concept_media':
                raise Conflict('Media-only validation forbids text rewrite: '+value['operation'])
        guard = call_guard.set(scope)
        steps = []
        try:
            app = Application(store, settings=Settings.from_env(ROOT), job_kinds=['BUILD_CONCEPT'])
            while result := app.run_once('workspace-existing-media'):
                steps.append(result)
                if result['status'] != 'SUCCEEDED':
                    raise Conflict('Media update failed: '+canonical(result))
        finally:
            call_guard.reset(guard)
        current = store.get_note(note['id'])
        def prose(value):
            return '\n'.join(line.strip() for line in IMAGE.sub('', value).splitlines() if line.strip())
        if prose(old['markdown']) != prose(current['markdown']):
            raise Conflict('Media refresh changed prose')
        with store.db.engine.connect() as c:
            usage = [dict(r) for r in rows(c, 'SELECT operation,model,input_tokens,output_tokens,total_tokens,elapsed_ms FROM provider_calls WHERE rowid>:n', n=baseline)]
            heads = [tuple(r) for r in c.exec_driver_sql("SELECT n.id,n.current_revision_id,r.markdown_sha256 FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='ATOMIC' ORDER BY n.id")]
        if atomic_hashes != heads:
            raise Conflict('Atomic heads changed')
        repeated = request_media(store, note['id'], old['revision']['id'])
        report = {'passed': True, 'title': title, 'note_id': note['id'], 'request': request,
            'old_revision': old['revision']['id'], 'current_revision': current['revision']['id'],
            'old_images': len(IMAGE.findall(old['markdown'])), 'current_images': len(IMAGE.findall(current['markdown'])),
            'prose_sha256': sha(prose(current['markdown'])), 'prose_unchanged': True,
            'atomic_heads_unchanged': True, 'repeat_idempotent': repeated['reused'],
            'elapsed_s': round(time.monotonic()-started,3), 'usage': usage, 'steps': steps}
        (run / 'media-acceptance.json').write_text(canonical(report), encoding='utf8')
        return report
    finally:
        store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--title', default='Transformer')
    args = parser.parse_args()
    print(json.dumps(validate(args.run.resolve(), args.title), ensure_ascii=False))
