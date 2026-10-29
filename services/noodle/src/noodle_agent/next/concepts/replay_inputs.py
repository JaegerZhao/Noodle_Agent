"""Read-only, eligibility-bound Atomic inputs in published reading order."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine

from ..contracts import canonical, sha
from ..eligibility import eligible
from ..reading_manifest import manifest
from ..repository import Conflict


class ReadOnlyLibrary:
    """Never initialize/migrate the inspected production database."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        uri = (self.root / 'noodle.db').as_uri() + '?mode=ro'
        self.engine = create_engine('sqlite://', creator=lambda: sqlite3.connect(uri, uri=True))
        self.db = SimpleNamespace(engine=self.engine)
        with self.engine.connect() as connection:
            policy = connection.exec_driver_sql('SELECT active_policy FROM storage_contract WHERE id=1').scalar_one()
        self.repository = SimpleNamespace(policy_version=policy)

    def read(self, ref, digest):
        path = (self.root / ref).resolve()
        if not path.is_relative_to(self.root):
            raise Conflict('Input artifact escaped library')
        data = path.read_bytes()
        if sha(data) != digest:
            raise Conflict('Input artifact hash differs: ' + ref)
        return data

    def close(self):
        self.engine.dispose()


def ordered_inputs(root, source_order=None):
    library = ReadOnlyLibrary(root)
    try:
        with library.engine.connect() as connection:
            valid = [n for n in eligible(connection) if n['kind'] == 'ATOMIC']
            sources = [dict(r) for r in connection.exec_driver_sql(
                "SELECT * FROM sources WHERE deleted_at IS NULL AND kind='PDF'").mappings()]
        if source_order:
            rank = {identity: i for i, identity in enumerate(source_order)}
            if set(rank) != {s['id'] for s in sources}:
                raise Conflict('Explicit source order must cover exactly the current PDF sources')
        else:
            def source_rank(source):
                title = source['title'].casefold()
                if 'probabilistic machine learning' in title:
                    return 0
                if 'attention' in title:
                    return 1
                if 'slides5' in title.replace('-', '').replace(' ', ''):
                    return 2
                if 'slides6' in title.replace('-', '').replace(' ', ''):
                    return 3
                raise Conflict('Provide an explicit order for unrecognized source: ' + source['title'])
            rank = {s['id']: source_rank(s) for s in sources}
            if len(set(rank.values())) != len(rank):
                raise Conflict('Source order is ambiguous')
        valid_by_id = {n['id']: n for n in valid}
        groups, seen = [], set()
        for source in sorted(sources, key=lambda s: rank[s['id']]):
            reading = manifest(library, source['current_revision_id'])
            nodes = sorted(reading.get('outline', {}).get('nodes', []), key=lambda n: n['position'])
            positions = {n['node_id']: i for i, n in enumerate(nodes)}
            titles = {n['node_id']: n['title'] for n in nodes}
            planned = {p['note_id']: i for i, p in enumerate(reading['notes'])}
            entries = sorted(reading['notes'], key=lambda n: (
                positions.get(n.get('primary_node_id'), len(positions)),
                min(n.get('pages') or [10**9]), planned[n['note_id']]))
            items = []
            for entry in entries:
                identity = entry['note_id']
                if identity not in valid_by_id:
                    raise Conflict('Reading manifest contains ineligible Atomic: ' + identity)
                if identity in seen:
                    raise Conflict('Atomic appears twice in frozen input: ' + identity)
                note = valid_by_id[identity]
                body = library.read(note['markdown_ref'], note['markdown_sha256'])
                proof_ref = 'artifacts/builds/' + note['revision_id'] + '.json'
                proof = (library.root / proof_ref).read_bytes()
                candidate = json.loads(proof)['candidate']
                assets = sorted(set(__import__('re').findall(r'!\[[^\]]*\]\((?:\.\./)?(assets/[a-f0-9]{64}\.[a-z]+)\)', body.decode())))
                for asset in assets:
                    path = library.root / asset
                    if not path.is_file() or sha(path.read_bytes()) != path.stem:
                        raise Conflict('Frozen image missing or hash differs: ' + asset)
                items.append({'note_id': identity, 'revision_id': note['revision_id'], 'title': note['title'],
                    'markdown_ref': note['markdown_ref'], 'markdown_sha256': note['markdown_sha256'],
                    'proof_ref': proof_ref, 'proof_sha256': sha(proof), 'pages': entry.get('pages', []),
                    'chapter_key': entry.get('chapter_key') or entry['key'],
                    'chapter_title': titles.get(entry.get('chapter_key'), entry['title']),
                    'outline_node_id': entry.get('primary_node_id'), 'position': len(items),
                    'section_ids': [s['section_id'] for s in candidate['sections']], 'assets': assets})
                seen.add(identity)
            groups.append({'source_id': source['id'], 'source_revision_id': source['current_revision_id'],
                'title': source['title'], 'kind': reading.get('document_profile', {}).get('kind'),
                'role': 'FOUNDATION' if reading.get('document_profile', {}).get('kind') == 'BOOK' else 'SUPPLEMENT',
                'position': len(groups), 'atomics': items})
        if seen != set(valid_by_id):
            raise Conflict('Eligible Atomics missing from current source manifests: ' + canonical(sorted(set(valid_by_id) - seen)))
        value = {'policy': 'ordered-atomic-replay-v1', 'sources': groups, 'atomic_count': len(seen)}
        return {**value, 'input_sha256': sha(canonical(value))}
    finally:
        library.close()
