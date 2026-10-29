"""Folder/display metadata and idempotent source lifecycle batches."""

import json
import re
import unicodedata

from sqlalchemy import text

from .contracts import canonical, sha
from .repository import Conflict, InvalidKnowledge, identifier, insert, now, one, rows


def label(value):
    value = unicodedata.normalize('NFKC', value).strip()
    if not value or len(value) > 500 or any(ord(c) < 32 for c in value):
        raise InvalidKnowledge('名称不能为空、过长或包含控制字符')
    return value


def natural_key(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part) for part in re.split(r'(\d+)', value.casefold()))


def folders(store):
    with store.db.engine.connect() as c:
        data = rows(c, '''SELECT f.*,count(s.id) source_count FROM source_folders f
            LEFT JOIN source_library_metadata m ON m.folder_id=f.id
            LEFT JOIN sources s ON s.id=m.source_id AND s.deleted_at IS NULL
            GROUP BY f.id ORDER BY f.name_key,f.id''')
        return {'items': [dict(r) for r in data]}


def folder_write(store, name, identity=None, expected_version=None):
    name = label(name)
    with store.repository.transaction() as c:
        if one(c, 'SELECT id FROM source_folders WHERE name_key=:k AND id!=:id', k=name.casefold(), id=identity or ''):
            raise Conflict('已存在同名文件夹')
        stamp = now()
        if identity:
            current = one(c, 'SELECT * FROM source_folders WHERE id=:id', id=identity)
            if not current or current['metadata_version'] != expected_version:
                raise Conflict('文件夹已变化，请刷新后重试')
            if current['name'] == name:
                return dict(current)
            c.execute(text('UPDATE source_folders SET name=:n,name_key=:k,metadata_version=metadata_version+1,updated_at=:t WHERE id=:id'),
                      {'n': name, 'k': name.casefold(), 't': stamp, 'id': identity})
        else:
            identity = identifier()
            insert(c, 'source_folders', id=identity, name=name, name_key=name.casefold(),
                   created_at=stamp, updated_at=stamp, metadata_version=1)
        return dict(one(c, 'SELECT * FROM source_folders WHERE id=:id', id=identity))


def folder_delete(store, identity, expected_version):
    with store.repository.transaction() as c:
        row = one(c, 'SELECT * FROM source_folders WHERE id=:id', id=identity)
        if not row or row['metadata_version'] != expected_version:
            raise Conflict('文件夹已变化，请刷新后重试')
        c.execute(text('UPDATE source_library_metadata SET folder_id=NULL,metadata_version=metadata_version+1,updated_at=:t WHERE folder_id=:id'), {'t': now(), 'id': identity})
        c.execute(text('DELETE FROM source_folders WHERE id=:id'), {'id': identity})
        return {'deleted': True, 'sources_moved_to': 'unfiled'}


def metadata(c, source_id):
    row = one(c, 'SELECT * FROM source_library_metadata WHERE source_id=:id', id=source_id)
    return dict(row) if row else {'source_id': source_id, 'folder_id': None, 'display_title': None, 'metadata_version': 0}


def _set_metadata(c, source_id, changes, expected_version):
    if not one(c, 'SELECT id FROM sources WHERE id=:id', id=source_id):
        raise InvalidKnowledge('资料不存在')
    current = metadata(c, source_id)
    if current['metadata_version'] != expected_version:
        raise Conflict('资料位置或名称已变化，请刷新后重试')
    if 'folder_id' in changes and changes['folder_id'] is not None and not one(c, 'SELECT id FROM source_folders WHERE id=:id', id=changes['folder_id']):
        raise Conflict('目标文件夹不存在')
    if 'display_title' in changes:
        changes = {**changes, 'display_title': label(changes['display_title'])}
    if all(current.get(k) == v for k, v in changes.items()):
        return current
    value = {**current, **changes, 'metadata_version': expected_version + 1, 'updated_at': now()}
    c.execute(text('''INSERT INTO source_library_metadata(source_id,folder_id,display_title,metadata_version,updated_at)
        VALUES(:source_id,:folder_id,:display_title,:metadata_version,:updated_at)
        ON CONFLICT(source_id) DO UPDATE SET folder_id=excluded.folder_id,display_title=excluded.display_title,
        metadata_version=excluded.metadata_version,updated_at=excluded.updated_at'''), value)
    return value


def update_metadata(store, identity, changes, expected_version):
    with store.repository.transaction() as c:
        return _set_metadata(c, identity, changes, expected_version)


def list_sources(store, *, folder='all', q='', sort='created_desc', limit=None, offset=0, include_deleted=True):
    if sort not in {'created_desc', 'created_asc', 'title_asc', 'title_desc'}:
        raise InvalidKnowledge('未知排序方式')
    with store.db.engine.connect() as c:
        data = [dict(r) for r in rows(c, '''SELECT s.*,r.sha256,r.original_name,r.byte_size,r.created_at imported_at,
            m.folder_id,m.display_title,coalesce(m.metadata_version,0) metadata_version
            FROM sources s JOIN source_revisions r ON r.id=s.current_revision_id
            LEFT JOIN source_library_metadata m ON m.source_id=s.id''')]
        from .eligibility import VALID
        counts = {r[0]: r[1] for r in c.exec_driver_sql(VALID + '''
            SELECT substr(n.creation_key,8,36),count(*) FROM atomic_valid v
            JOIN note_revisions r ON r.id=v.id JOIN notes n ON n.id=r.note_id
            GROUP BY substr(n.creation_key,8,36)''')}
    for item in data:
        item['display_title'] = item['display_title'] or item['title']
        item['atomic_count'] = counts.get(item['current_revision_id'], 0)
    selected = [s for s in data if (include_deleted or not s['deleted_at'])
                and (folder == 'all' or (s['folder_id'] is None if folder == 'unfiled' else s['folder_id'] == folder))
                and (not q or q.casefold() in s['display_title'].casefold() or q.casefold() in s['title'].casefold())]
    selected.sort(key=lambda s: ((natural_key(s['display_title']) if sort.startswith('title') else s['imported_at']), s['id']), reverse=sort.endswith('desc'))
    return {'items': selected[offset:offset + limit if limit is not None else None], 'total': len(selected)}


def batch(store, operation_id, action, items, folder_id=None):
    if action not in {'move', 'delete', 'restore'} or not items or len(items) > 500:
        raise InvalidKnowledge('无效的批量操作')
    if len({x['source_id'] for x in items}) != len(items):
        raise InvalidKnowledge('批量操作包含重复资料')
    digest = sha(canonical({'action': action, 'items': items, 'folder_id': folder_id}))
    with store.repository.transaction() as c:
        old = one(c, 'SELECT * FROM source_library_operations WHERE operation_id=:id', id=operation_id)
        if old:
            if old['request_sha256'] != digest:
                raise Conflict('同一操作标识不能提交不同内容')
            return json.loads(old['result_json'])
        results, changed = [], []
        for item in items:
            identity = item['source_id']
            try:
                with c.begin_nested():
                    if action == 'move':
                        value = _set_metadata(c, identity, {'folder_id': folder_id}, item['expected_metadata_version'])
                    else:
                        value = store.sources.set_source_deleted(identity, action == 'delete', expected_revision=item['expected_revision'], _connection=c, _emit=False)
                        if value['changed']:
                            changed.append(identity)
                    results.append({'source_id': identity, 'status': 'SUCCEEDED', 'result': value})
            except (Conflict, InvalidKnowledge, KeyError) as exc:
                results.append({'source_id': identity, 'status': 'CONFLICT', 'detail': str(exc)})
        if changed:
            store.repository.advance_epoch(c)
            store.repository.event(c, 'SOURCES_STATE_CHANGED', 'source-library', None,
                {'source_ids': changed, 'action': action}, key='source-batch:' + operation_id)
        result = {'operation_id': operation_id, 'items': results,
                  'succeeded': sum(x['status'] == 'SUCCEEDED' for x in results)}
        insert(c, 'source_library_operations', operation_id=operation_id, request_sha256=digest,
               result_json=canonical(result), created_at=now())
        return result
