"""A cold replay must contain valid frozen Atomic inputs, not old answers."""

import importlib.util
import sqlite3
from pathlib import Path

from test_next_workspace import atomic, concept, store  # noqa: F401

from noodle_agent.next.contracts import sha
from noodle_agent.next.store import Store


def test_import_skips_withdrawn_atomics_and_all_concept_answers(store, tmp_path):  # noqa: F811
    kept, _ = atomic(store)
    removed, _ = atomic(store, '撤回输入', parts=('独立来源。',))
    concept(store, kept)
    store.withdraw_note(removed['note_id'], expected_revision=removed['revision_id'])
    script = Path(__file__).resolve().parents[3] / 'scripts/concept_validation_replay.py'
    spec = importlib.util.spec_from_file_location('replay_import_fixture', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / 'cold'
    result = module.import_atomics(store.root, target, Store, sha)
    assert [n['id'] for n in result['atomics']] == [kept['note_id']]
    with sqlite3.connect(target / 'noodle.db') as connection:
        assert connection.execute("SELECT count(*) FROM notes WHERE kind='CONCEPT'").fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM atomic_entity_routes').fetchone()[0] == 0
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
    assert (target / result['atomics'][0]['markdown_ref']).read_bytes() == store.path(result['atomics'][0]['markdown_ref']).read_bytes()
