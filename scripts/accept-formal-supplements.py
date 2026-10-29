"""Read back the exact formal textbook/slides/Attention validation without model calls."""

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/noodle/src'))

from noodle_agent.next.contracts import Candidate, Review, sha
from noodle_agent.next.eligibility import eligible
from noodle_agent.next.knowledge_tree import tree
from noodle_agent.next.metrics import timeline
from noodle_agent.next.navigation import frozen_candidate
from noodle_agent.next.pipeline_state import readiness
from noodle_agent.next.repository import one, rows
from noodle_agent.next.store import Store
from noodle_agent.next.verification import verify_current_generation, verify_generation

RUN=ROOT/'.local-only/concept-supplement-validation-20261027'


def main():
    baseline=json.loads((RUN/'baseline.json').read_text(encoding='utf-8'))
    store=Store(Path(baseline['library']))
    try:
        with store.db.engine.connect() as c:
            current=eligible(c)
            sources=rows(c,"SELECT s.id,s.title,r.id revision_id,r.sha256 FROM sources s JOIN source_revisions r ON r.id=s.current_revision_id WHERE s.kind='PDF' ORDER BY s.created_at")
            integrity=c.exec_driver_sql('PRAGMA integrity_check').scalar_one()
            foreign_keys=c.exec_driver_sql('PRAGMA foreign_key_check').fetchall()
            head=one(c,"SELECT g.* FROM index_heads h JOIN index_generations g ON g.id=h.generation WHERE h.mode_key LIKE 'HYBRID:%' AND g.status='READY' ORDER BY g.created_at DESC LIMIT 1")
            unsettled=[dict(r) for r in rows(c,"SELECT id,kind,status,error_json FROM jobs WHERE rowid>:b AND status IN ('PENDING','RUNNING','RETRY','REVIEW')",b=baseline['job_rowid'])]
        book_errors=[]
        for note in baseline['heads']:
            if note['kind']!='ATOMIC':
                continue
            now=store.get_note(note['id'])
            if now['revision']['id']!=note['current_revision_id'] or sha(now['markdown'])!=note['markdown_sha256']:
                book_errors.append(note['id'])
        proof_errors=[]
        for note in current:
            if note['kind']!='CONCEPT':
                continue
            try:
                with store.db.engine.connect() as c:
                    proof=one(c,'SELECT sha256 FROM build_artifacts WHERE storage_ref=:r',r=f"artifacts/builds/{note['revision_id']}.json")
                    raw=json.loads(store.read(f"artifacts/builds/{note['revision_id']}.json",proof['sha256']))
                    candidate,review=Candidate.model_validate(raw['candidate']),Review.model_validate(raw['review'])
                    if not review.passed or candidate.fingerprint!=review.candidate_sha256:
                        raise ValueError('Proof mismatch')
                    store.publisher.validate_inputs(c,note,candidate)
            except (ValueError,KeyError,TypeError,OSError) as exc:
                proof_errors.append({'note_id':note['id'],'error':str(exc)})
        spec=importlib.util.spec_from_file_location('measurements',ROOT/'scripts/formal-measurements.py')
        measurements=importlib.util.module_from_spec(spec);spec.loader.exec_module(measurements)
        manifest=json.loads((ROOT/'.local-only/formal-workspace-20261025T043825Z/manifest.json').read_text(encoding='utf-8'))
        documents=[]
        for source in sources:
            if source['id'] in baseline['source_ids']:
                continue
            state=readiness(store,source['revision_id'])
            versions=verify_current_generation(store,state['job_id'])
            original=verify_generation(store,state['job_id']) if not versions['automatic_repair_count'] else None
            timing=timeline(store,state['job_id'])
            material=next(d for d in manifest['documents'] if d['sha256']==source['sha256'])
            historical=measurements.historical_parse(material)
            notes=[]
            for item in versions['notes']:
                note=store.get_note(item['note_id'])
                candidate=frozen_candidate(store,note['revision']['id'])
                images=re.findall(r'!\[[^\]]*\]\(([^)]+)\)',note['markdown'])
                missing=[p for p in images if not p.startswith('../assets/') or not store.path(p.removeprefix('../')).is_file()]
                notes.append({**item,'characters':len(note['markdown']),'h1_count':len(re.findall(r'^# ',note['markdown'],re.MULTILINE)),
                    'section_titles':[s.title for s in candidate.sections], 'image_count':len(images),'missing_images':missing})
            documents.append({**dict(source),'readiness':state,'version_readback':versions,
                'original_readback':original,'notes':notes,'timing':{'summary':timing['summary'],'stages':timing['stages']},
                'historical_mineru':historical})
        entries=tree(store,'all',limit=3000)['entries']
        concept_ids={n['id'] for n in current if n['kind']=='CONCEPT'}
        manifest_items=json.loads(store.read(head['manifest_ref'],head['manifest_sha256'])) if head else []
        indexed={v['revision_id'] for v in manifest_items}
        result={'recorded_at':datetime.now(UTC).isoformat(),'book_atomics_unchanged':not book_errors,
            'book_errors':book_errors,'integrity':integrity,'foreign_keys':[list(r) for r in foreign_keys],
            'proof_errors':proof_errors,'index_current':indexed=={n['revision_id'] for n in current},
            'index_generation':head['id'] if head else None,'indexed_chunks':len(manifest_items),
            'tree_complete':{e['concept_id'] for e in entries}==concept_ids and len(entries)==len(concept_ids),
            'current_concepts':len(concept_ids),'new_unsettled_jobs':unsettled,'documents':documents,
            'scope':'Engineering, version/evidence and publication checks; no claim of exhaustive scientific verification',
            'model_calls':0}
        result['engineering_passed']=(not book_errors and integrity=='ok' and not foreign_keys and not proof_errors
            and result['index_current'] and result['tree_complete'] and not unsettled and len(documents)==2
            and all(d['version_readback']['passed'] and d['readiness']['notes_status']=='READY'
                and d['readiness']['concept_maintenance']['status']=='COMPLETE'
                and all(n['h1_count']==1 and not n['missing_images'] for n in d['notes']) for d in documents))
        destination=RUN/('acceptance-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.json')
        destination.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k!='documents'}|{'report':str(destination),
            'documents':[{'title':d['title'],'atomic_count':d['version_readback']['current_atomic_count'],
                'notes_status':d['readiness']['notes_status'],'concept_status':d['readiness']['concept_maintenance']['status'],
                'historical_mineru':d['historical_mineru']} for d in documents]},ensure_ascii=False,indent=2))
    finally:
        store.close()


if __name__=='__main__':
    main()
