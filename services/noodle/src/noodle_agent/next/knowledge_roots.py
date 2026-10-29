"""Multiple navigation roots reuse versioned maps; no graph file is authoritative."""

from .contracts import sha
from .navigation import current_notes
from .repository import rows


def roots(store, *, _notes=None):
    notes = _notes if _notes is not None else current_notes(store, limit=100000)['items']
    concepts = [n for n in notes if n['kind']=='CONCEPT']
    maps = [n for n in notes if n['kind']=='MOC']
    valid_ids = {n['id'] for n in concepts}
    with store.db.engine.connect() as c:
        entries = rows(c, "SELECT m.map_revision_id,m.target_note_id FROM map_entries m JOIN notes n ON n.current_revision_id=m.map_revision_id WHERE n.creation_key LIKE 'knowledge-root:%'")
        revision_times={r['note_id']:r['created_at'] for r in rows(c,'SELECT r.note_id,r.created_at FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id')}
    members = {}
    for entry in entries:
        if entry['target_note_id'] in valid_ids:
            members.setdefault(entry['map_revision_id'], set()).add(entry['target_note_id'])
    result = [{'id': 'all', 'title': '全部知识', 'kind': 'AUTOMATIC', 'count': len(concepts)}]
    domains = sorted({'/'.join(parts[:i]) for n in concepts for parts in [[p.strip() for p in n['domain'].split('/') if p.strip()]] for i in range(1,len(parts)+1)})
    for domain in domains:
        result.append({'id': 'domain-' + sha(domain)[:20], 'title': domain, 'kind': 'DOMAIN', 'count': sum(n['domain'] == domain or n['domain'].startswith(domain + '/') for n in concepts)})
    for note in maps:
        if (note.get('creation_key') or '').startswith('knowledge-root:'):
            result.append({'id': note['id'], 'title': note['title'], 'kind': 'PERSONAL', 'map_note_id': note['id'],
                           'revision_id': note['current_revision_id'], 'count': len(members.get(note['current_revision_id'], set())),
                           'description': note.get('summary', '')[:160]})
    for item in result:
        selected = [n for n in concepts if item['kind']=='AUTOMATIC' or item['kind']=='DOMAIN' and (n['domain']==item['title'] or n['domain'].startswith(item['title']+'/'))
                    or item['kind']=='PERSONAL' and n['id'] in members.get(item['revision_id'], set())]
        item['sample_titles'] = [n['title'] for n in selected[:4]]
        item['updated_at'] = max((revision_times[n['id']] for n in selected), default=None)
    return {'schema_version': 'knowledge-roots-v1', 'items': result}


def tree(store, identity, **kwargs):
    from .knowledge_tree import tree as project
    return project(store, identity, **kwargs)


def membership(store, identity, **kwargs):
    from .knowledge_tree import membership as resolve
    return resolve(store, identity, **kwargs)


def member_cards(store, identity, *, limit=60, offset=0, q=''):
    from .card_preview import stored_excerpt
    notes = current_notes(store, limit=100000)['items']
    root, selected = membership(store, identity, _notes=notes)
    chosen = [n for n in notes if n['id'] in selected and (not q or q.casefold() in (n['title']+' '+n['summary']).casefold())]
    version = sha(str(sorted((n['id'], n['current_revision_id']) for n in chosen)))
    page=[{**n,'preview_markdown':stored_excerpt(str(store.root),n['markdown_ref'],n['markdown_sha256'],n['title'])} for n in chosen[offset:offset+limit]]
    return {'root': root, 'snapshot': version, 'items': page, 'total': len(chosen),
            'next_offset': offset+limit if offset+limit<len(chosen) else None}
