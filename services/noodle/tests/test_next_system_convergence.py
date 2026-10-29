"""Regression checks for transaction and current-scope maintenance boundaries."""

import copy
import sqlite3
from types import SimpleNamespace

import pytest
from test_next_organization_runtime import enqueue
from test_next_source_sections import prepared  # noqa: F401

from noodle_agent.next.concepts.entity_routing import ensure_plan
from noodle_agent.next.organization import (
    coverage,
    neighborhood,
    organization_graph,
    scope_signature,
)
from noodle_agent.next.store import Store


def test_catalogue_reads_do_not_hold_the_sqlite_writer_lock(prepared, monkeypatch):  # noqa: F811
    store, lease, _ = prepared
    monkeypatch.setattr('noodle_agent.next.concepts.entity_routing.cohort', lambda *_: (lease.input['revision_id'], []))

    def catalogue(*_):
        with sqlite3.connect(store.root / 'noodle.db', timeout=.1) as second_writer:
            second_writer.execute('BEGIN IMMEDIATE')
            second_writer.rollback()
        raise RuntimeError('catalogue reached without writer lock')

    monkeypatch.setattr('noodle_agent.next.concepts.analysis.catalogue', catalogue)
    with pytest.raises(RuntimeError, match='without writer lock'):
        ensure_plan(store, lease, {}, SimpleNamespace(policy=None))


def test_only_failure_bound_to_current_graph_counts_as_current(tmp_path):
    store = Store(tmp_path / 'coverage')
    try:
        enqueue(store, 'current')
        lease = store.jobs.claim('one', kind='REVIEW_ORGANIZATION')
        graph = organization_graph(store)
        store.jobs.fail(lease, {'organization_snapshot': {'reviewed_graph_snapshot': graph['snapshot'],
            'reviewed_scope_snapshot': scope_signature(graph)}}, retry=False)
        assert coverage(store)['failed'] == 1
        enqueue(store, 'unbound-history')
        lease = store.jobs.claim('two', kind='REVIEW_ORGANIZATION')
        store.jobs.fail(lease, {'code': 'OLD_UNKNOWN_SCOPE'}, retry=False)
        result = coverage(store)
        assert result['failed'] == 1 and result['historical_failed'] == 1
    finally:
        store.close()


def test_unrelated_graph_change_does_not_erase_a_local_scope_proof(tmp_path, monkeypatch):
    store = Store(tmp_path / 'local')
    graph = {'snapshot': 'before', 'nodes': [
        {'id': f'c{i}', 'title': f'Object{i}', 'description': ''} for i in range(9)], 'edges': []}
    monkeypatch.setattr('noodle_agent.next.organization.organization_graph', lambda _: graph)
    try:
        focus = {'c0'}
        local = neighborhood(graph, focus)
        outsider = next(n for n in graph['nodes'] if n['id'] not in {v['id'] for v in local['nodes']})
        enqueue(store, 'local')
        lease = store.jobs.claim('one', kind='REVIEW_ORGANIZATION')
        store.jobs.finish(lease, {'scope': 'CHANGED_CONCEPT_NEIGHBORHOOD', 'concept_ids': sorted(focus),
            'reviewed_graph_snapshot': 'before', 'reviewed_scope_snapshot': scope_signature(local)})
        graph = copy.deepcopy(graph)
        graph['snapshot'] = 'after'
        next(n for n in graph['nodes'] if n['id'] == outsider['id'])['description'] = 'unrelated update'
        result = coverage(store)
        assert result['local_status'] == 'COMPLETE' and result['status'] != 'COMPLETE'
    finally:
        store.close()
