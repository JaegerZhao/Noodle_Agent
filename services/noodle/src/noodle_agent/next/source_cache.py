"""Verified local MinerU reuse; external library reuse remains opt-in."""

import copy
import json
import os
import sqlite3
from pathlib import Path

from .contracts import sha
from .planning import QualityFailure

PARSE_FIELDS = ('parser', 'parse_mode', 'parse_model', 'language')


def parser_only_artifact(raw):
    artifact, removed = copy.deepcopy(raw), 0
    for block in artifact['blocks']:
        visual = block.pop('visual', None)
        if visual is not None or block.get('evidence_origin') == 'DERIVED_VISION':
            block['text'] = block.get('text', '').split('[视觉识别，需结合原图核对]', 1)[0].rstrip()
            block.pop('evidence_origin', None)
            removed += 1
    return artifact, removed


def configured_roots():
    value = json.loads(os.getenv('NOODLE_NEXT_PARSE_CACHE_LIBRARIES', '[]'))
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise QualityFailure('Parse cache libraries must be a JSON array of explicit local directories')
    return [Path(v).resolve() for v in value]


def reuse_parse(store, journal, source, profile, inputs, roots=None):
    candidates = [store.root, *(configured_roots() if roots is None else roots)]
    seen = set()
    for root in candidates:
        root = Path(root).resolve()
        if root in seen or not (root / 'noodle.db').is_file():
            continue
        seen.add(root)
        with sqlite3.connect((root / 'noodle.db').as_uri() + '?mode=ro', uri=True) as c:
            values = c.execute("SELECT sr.blob_ref,k.artifact_ref,k.artifact_sha256 FROM source_revisions sr JOIN jobs j ON json_extract(j.input_json,'$.revision_id')=sr.id JOIN job_checkpoints k ON k.job_id=j.id WHERE sr.sha256=? AND k.stage='parse.normalized' AND k.state='DONE' ORDER BY k.created_at DESC", (source['sha256'],)).fetchall()
        for blob, ref, expected in values:
            def read(key, digest, root=root):
                p = (root / key).resolve()
                if not p.is_relative_to(root) or not p.is_file():
                    raise QualityFailure('Parse cache reference escapes its library or is missing')
                data = p.read_bytes()
                if sha(data) != digest:
                    raise QualityFailure('Parse cache bytes differ from their receipt')
                return data

            read(blob, source['sha256'])
            raw = json.loads(read(ref, expected))['artifact']
            original = raw.get('profile', {})
            if not all(original.get(k) == profile.get(k) for k in PARSE_FIELDS):
                continue
            artifact, stripped_visuals = parser_only_artifact(raw)
            for block in artifact['blocks']:
                if block.get('asset_ref'):
                    asset = block['asset_ref']
                    if not asset.startswith('assets/'):
                        raise QualityFailure('Cached asset is not content addressed')
                    store.put(asset, read(asset, Path(asset).stem))
            provenance = {'policy': 'verified-mineru-reuse-v1', 'origin_library': str(root), 'origin_ref': ref,
                'origin_sha256': expected, 'source_sha256': source['sha256'], 'parse_profile': {k: original.get(k) for k in PARSE_FIELDS},
                'legacy_visual_annotations_removed': stripped_visuals, 'vision_reused': False}
            parse_id = store.sources.record_parse(source['id'], raw.get('parser_version') or 'mineru', provenance, artifact, lease=journal.lease)
            journal.save('parse.reuse', {'source_sha256': source['sha256'], 'parse_profile': provenance['parse_profile']}, provenance)
            return journal.save('parse.normalized', inputs, {'parse_id': parse_id, 'artifact': artifact})
    return None
