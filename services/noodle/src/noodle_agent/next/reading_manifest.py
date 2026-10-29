"""Fast reading manifests and immutable source anchors; no readiness/graph work."""

import json
from functools import lru_cache
from pathlib import Path
from threading import RLock

from .contracts import canonical, sha
from .repository import Conflict, one, rows

_context_lock = RLock()


@lru_cache(maxsize=128)
def frozen_json(root, ref, digest):
    data = (Path(root) / ref).read_bytes()
    if sha(data) != digest:
        raise Conflict('Reading artifact hash differs')
    return json.loads(data)


def state(store, revision):
    with store.db.engine.connect() as c:
        source = one(c, "SELECT r.id,r.source_id,s.title FROM source_revisions r JOIN sources s ON s.id=r.source_id WHERE r.id=:r AND s.current_revision_id=r.id AND s.deleted_at IS NULL", r=revision)
        if not source:
            raise Conflict('Source revision is not current and available')
        job = one(c, "SELECT id,status,error_json FROM jobs WHERE kind='GENERATE_ATOMICS' AND json_extract(input_json,'$.revision_id')=:r ORDER BY created_at DESC,id DESC LIMIT 1", r=revision)
        checkpoints = rows(c, "SELECT stage,artifact_ref,artifact_sha256 FROM job_checkpoints WHERE job_id=:j AND state='DONE' AND (stage='units' OR stage LIKE 'source.sections.plan%' OR stage LIKE 'source.publish.%') ORDER BY created_at DESC", j=job['id']) if job else []
        # Same Atomic eligibility rules as ContentRepository.valid_revision,
        # checked in one query rather than per evidence anchor or per note.
        notes = rows(c, """SELECT n.id,n.title,n.creation_key,n.current_revision_id FROM notes n
            JOIN note_revisions r ON r.id=n.current_revision_id
            JOIN storage_contract sc ON sc.id=1
            WHERE n.kind='ATOMIC' AND n.visibility='ACTIVE' AND r.status='PUBLISHED'
            AND r.policy_version=:policy AND sc.active_policy=:policy
            AND substr(n.creation_key,1,length(:prefix))=:prefix
            AND EXISTS(SELECT 1 FROM section_evidence se WHERE se.revision_id=r.id)
            AND NOT EXISTS(SELECT 1 FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id
                LEFT JOIN source_revisions sr ON sr.id=e.source_revision_id LEFT JOIN sources s ON s.id=sr.source_id
                WHERE se.revision_id=r.id AND (s.id IS NULL OR s.deleted_at IS NOT NULL OR s.current_revision_id!=sr.id))
            AND NOT EXISTS(SELECT 1 FROM author_origin_sources a
                LEFT JOIN source_revisions sr ON sr.id=a.source_revision_id LEFT JOIN sources s ON s.id=sr.source_id
                WHERE a.revision_id=r.id AND (s.id IS NULL OR s.deleted_at IS NOT NULL OR s.current_revision_id!=sr.id))
            ORDER BY n.id""", policy=store.repository.policy_version, prefix=f'atomic:{revision}:')
    version = sha(canonical({'source':dict(source), 'job':dict(job) if job else None,
        'receipts':[(v['stage'],v['artifact_sha256']) for v in checkpoints], 'notes':[dict(n) for n in notes]}))
    return source, job, checkpoints, notes, version


def manifest(store, revision, *, version_only=False):
    source, job, checkpoints, notes, version = state(store, revision)
    if version_only:
        return {'version':version}
    receipt = next((v for v in checkpoints if v['stage'].startswith('source.sections.plan')), None)
    if not receipt:
        from .source_structure import source_structure
        return {**source_structure(store, revision), 'version':version}
    from .learning_titles import reading_title
    root = str(store.root)
    plan = frozen_json(root, receipt['artifact_ref'], receipt['artifact_sha256'])
    outline = plan.get('source_outline', plan['outline']) if plan.get('document_profile', {}).get('kind') == 'BOOK' else plan['outline']
    unit_receipt = next((v for v in checkpoints if v['stage']=='units'), None)
    binding = sha(receipt['artifact_sha256'] + (unit_receipt['artifact_sha256'] if unit_receipt else '') + 'reading-owner-v3')
    resolved, deferred = set(), set()
    for cp in checkpoints:
        if cp['stage'].startswith('source.publish.'):
            value = frozen_json(root, cp['artifact_ref'], cp['artifact_sha256'])
            resolved.update(value.get('resolved_plan_keys', []))
            deferred.update(v['plan_key'] for v in value.get('deferred', []))
    deferred.difference_update(resolved)
    by_key = {n['creation_key']:n for n in notes}
    published, planned = [], []
    for p in plan['plans']:
        key = f"atomic:{revision}:source-section:{p['key']}"
        note = by_key.get(key)
        primary = (p.get('outline_node_id') or p['key']) if plan.get('source_outline') and plan.get('document_profile', {}).get('kind') == 'BOOK' else p['key']
        item = {'key':p['key'], 'creation_key':key, 'title':p['title'], 'display_title':reading_title(p),
            'primary_node_id':primary, 'pages':p['pages'], 'chapter_key':p.get('chapter_key'),
            'note_id':note['id'] if note else None,
            'status':'PUBLISHED' if note else 'DEFERRED' if p['key'] in deferred else 'OMITTED' if p['key'] in resolved else 'PLANNED'}
        planned.append(item)
        if note:
            published.append({**item, 'note_id':note['id'], 'revision_id':note['current_revision_id'],
                'title':note['title'], 'display_title':reading_title(p) if note['title']==p['title'] else note['title'],
                'context_node_ids':[], 'section_anchors':[]})
    nodes = [{**n, 'source_title':n['title'], 'title':plan.get('display_titles', {}).get(n['node_id'], n['title']),
        'status':'NOT_PLANNED', 'planned_count':0, 'published_count':0} for n in outline['nodes']]
    by_node = {n['node_id']:n for n in nodes}
    for p in planned:
        owner = p['primary_node_id']
        while owner in by_node:
            node = by_node[owner];node['planned_count'] += 1;node['published_count'] += p['status']=='PUBLISHED'
            owner = node['parent_id']
    for node in nodes:
        node['status'] = 'COMPLETE' if node['planned_count'] and node['published_count']==node['planned_count'] else 'PARTIAL' if node['published_count'] else 'PLANNED' if node['planned_count'] else 'NOT_PLANNED'
    return {'schema_version':'reading-manifest-v1', 'version':version, 'anchor_binding':binding,
        'source_id':source['source_id'], 'source_revision_id':revision, 'display_title':plan.get('display_title',''),
        'outline':{**outline,'nodes':nodes}, 'notes':published, 'planned_notes':planned,
        'planning_status':'COMPLETE_SOURCE_SECTIONS', 'generation_status':job['status'] if job else None,
        'document_profile':plan.get('document_profile',{}), 'chapter_tasks':plan.get('chapter_tasks',[]),
        'counts':{'published':len(published),'planned':len(planned)}}


@lru_cache(maxsize=8)
def _anchor_context(root, plan_ref, plan_sha, units_ref, units_sha):
    from .document_outline import DocumentOutline
    from .generation_contracts import Unit
    from .reading_projection import anchor_context
    plan = frozen_json(root, plan_ref, plan_sha)
    outline = DocumentOutline.model_validate(plan.get('source_outline',plan['outline']) if plan.get('document_profile',{}).get('kind')=='BOOK' else plan['outline'])
    units = [Unit.model_validate(v) for v in frozen_json(root,units_ref,units_sha)['units']]
    return anchor_context(outline, units)


def note_anchors(store, source_revision, note_revision):
    from .reading_projection import section_anchors
    _, _, checkpoints, _, _ = state(store, source_revision)
    plan = next((v for v in checkpoints if v['stage'].startswith('source.sections.plan')), None)
    units = next((v for v in checkpoints if v['stage']=='units'), None)
    if not plan or not units:
        return []
    with _context_lock:
        context = _anchor_context(str(store.root),plan['artifact_ref'],plan['artifact_sha256'],units['artifact_ref'],units['artifact_sha256'])
    anchors = section_anchors(store,note_revision,None,None,context=context)
    frozen = frozen_json(str(store.root), plan['artifact_ref'], plan['artifact_sha256'])
    if frozen.get('document_profile', {}).get('kind') != 'BOOK':
        with store.db.engine.connect() as c:
            note = one(c, 'SELECT n.creation_key FROM note_revisions r JOIN notes n ON n.id=r.note_id WHERE r.id=:r', r=note_revision)
        key = note['creation_key'].split(':source-section:',1)[-1] if note else None
        owner = next((p['key'] for p in frozen['plans'] if p['key']==key), None)
        if owner:
            anchors = [{**a, 'source_node_id':a['node_id'], 'node_id':owner, 'reading_owner':owner} for a in anchors]
    return anchors


@lru_cache(maxsize=2048)
def image_size(path):
    import struct
    with Path(path).open('rb') as stream:
        header = stream.read(32)
        if header.startswith(b'\x89PNG\r\n\x1a\n') and len(header)>=24:
            return list(struct.unpack('>II',header[16:24]))
        if header[:2]==b'\xff\xd8':
            stream.seek(2)
            while True:
                marker=stream.read(1)
                if not marker:return None
                if marker!=b'\xff':continue
                marker=stream.read(1)
                while marker==b'\xff':marker=stream.read(1)
                if not marker or marker in {b'\xda',b'\xd9'}:return None
                if marker==b'\x00' or 0xd0<=marker[0]<=0xd7:continue
                length=stream.read(2)
                if len(length)!=2:return None
                size=int.from_bytes(length,'big')
                if marker[0] in {0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf}:
                    data=stream.read(5)
                    return [int.from_bytes(data[3:5],'big'),int.from_bytes(data[1:3],'big')] if len(data)==5 else None
                if size<2:return None
                stream.seek(size-2,1)
        if header[:4]==b'RIFF' and header[8:12]==b'WEBP':
            if header[12:16]==b'VP8X':return [int.from_bytes(header[24:27],'little')+1,int.from_bytes(header[27:30],'little')+1]
            if header[12:16]==b'VP8 ' and header[23:26]==b'\x9d\x01\x2a':return [int.from_bytes(header[26:28],'little')&0x3fff,int.from_bytes(header[28:30],'little')&0x3fff]
            if header[12:16]==b'VP8L' and header[20:21]==b'\x2f':
                value=int.from_bytes(header[21:25],'little');return [(value&0x3fff)+1,((value>>14)&0x3fff)+1]
    return None


def note_image_sizes(store, markdown):
    import re
    result = {}
    for ref in set(re.findall(r'!?\[[^\]]*\]\(((?:\.\./)?assets/[a-f0-9]{64}\.(?:png|jpg|jpeg|webp))\)',markdown)):
        path = store.path(ref.removeprefix('../'))
        if path.is_file():
            size=image_size(str(path))
            if size and all(v>0 for v in size):result[ref] = size
    return result
