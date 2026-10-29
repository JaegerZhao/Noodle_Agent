"""Display change types from recorded publication receipts, never guesses from prose."""

import json

from .repository import one


def change_kind(store,item):
    author=item['author']
    if author.startswith(('author.','human.')):
        return 'AUTHOR'
    if 'format' in author:
        return 'FORMAT'
    if 'organization' in author:
        return 'STRUCTURE'
    ref='artifacts/builds/'+item['id']+'.json'
    with store.db.engine.connect() as c:
        artifact=one(c,'SELECT sha256 FROM build_artifacts WHERE storage_ref=:r',r=ref)
    if not artifact:
        return 'LEGACY'
    try:
        report=json.loads(store.read(ref,artifact['sha256']))['review']['report']
    except (FileNotFoundError,ValueError,KeyError):
        return 'LEGACY'
    checks=list(report.get('checks',{}).values())
    if any(v.get('media',{}).get('called') and not v.get('writer_called') for v in checks):
        return 'MEDIA'
    if any(v.get('writer_called') for v in checks):
        return 'CONTENT'
    if checks or report.get('metadata_only'):
        return 'SUPPORT'
    return 'LEGACY'
