"""One source-group projection for current or explicitly historical Concept inputs."""

import json

from sqlalchemy import bindparam, text

from .eligibility import eligible
from .repository import InvalidKnowledge, rows


def sources(store, identity, revision=None):
    historical=revision is not None
    note=store.get_note(identity,revision_id=revision,historical=bool(revision))
    if note['kind']!='CONCEPT':
        raise InvalidKnowledge('Source groups require a Concept')
    revision=note['revision']['id']
    with store.db.engine.connect() as c:
        current={n['revision_id'] for n in eligible(c)}
        selected={}
        for item in rows(c,'SELECT * FROM concept_inputs WHERE concept_revision_id=:r',r=revision):
            value=selected.setdefault(item['atomic_revision_id'],{'sections':set(),'purposes':set()})
            value['sections'].update(json.loads(item['selection_json']))
            value['purposes'].add('SUPPORT')
        for item in rows(c,"SELECT * FROM links WHERE from_revision_id=:r AND relation='LINKS_TO'",r=revision):
            basis=json.loads(item['basis_json'])
            if basis.get('origin')!='SYSTEM':
                continue
            for target in rows(c,"SELECT r.id FROM note_revisions r JOIN notes n ON n.id=r.note_id WHERE n.id=:id AND n.kind='ATOMIC'",id=item['target_note_id']):
                if target['id'] not in basis.get('support_revision_ids',[]):
                    continue
                value=selected.setdefault(target['id'],{'sections':set(),'purposes':set()})
                value['purposes'].add('FURTHER_READING')
                if item['target_section_id']:
                    value['sections'].add(item['target_section_id'])
        if not selected:
            return {'groups':[],'revision_id':revision}
        values=c.execute(text('''SELECT DISTINCT r.id atomic_revision,n.id atomic_id,r.title,
            se.section_id,e.source_revision_id,e.page_number,s.deleted_at,s.kind source_kind,
            coalesce(m.display_title,s.title) source_title,ar.route_json
            FROM note_revisions r JOIN notes n ON n.id=r.note_id
            JOIN section_evidence se ON se.revision_id=r.id
            JOIN evidence_anchors e ON e.id=se.evidence_id
            JOIN source_revisions sr ON sr.id=e.source_revision_id JOIN sources s ON s.id=sr.source_id
            LEFT JOIN source_library_metadata m ON m.source_id=s.id
            LEFT JOIN atomic_entity_routes ar ON ar.atomic_revision_id=r.id WHERE r.id IN :ids''')
            .bindparams(bindparam('ids',expanding=True)),{'ids':list(selected)}).mappings()
        groups={}
        for value in values:
            if not historical and value['atomic_revision'] not in current:
                continue
            selection=selected[value['atomic_revision']]
            if selection['sections'] and value['section_id'] not in selection['sections']:
                continue
            group=groups.setdefault(value['source_revision_id'],{'source_revision_id':value['source_revision_id'],
                'title':value['source_title'],'kind':value['source_kind'],'available':not value['deleted_at'],'atomics':{}})
            route=json.loads(value['route_json']) if value['route_json'] else {}
            entry=group['atomics'].setdefault(value['atomic_revision'],{'note_id':value['atomic_id'],
                'revision_id':value['atomic_revision'],'title':value['title'],'pages':set(),'section_ids':set(),
                'role':route.get('source_role','UNKNOWN'),'purposes':sorted(selection['purposes'])})
            if value['page_number']:
                entry['pages'].add(value['page_number'])
            entry['section_ids'].add(value['section_id'])
    result=[]
    for group in groups.values():
        group['atomics']=[{**a,'pages':sorted(a['pages']),'section_ids':sorted(a['section_ids'])} for a in group['atomics'].values()]
        result.append(group)
    return {'groups':result,'revision_id':revision}
