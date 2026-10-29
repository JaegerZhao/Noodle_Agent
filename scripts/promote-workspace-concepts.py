"""Prepare/apply a reviewed replay package through native publication and CAS."""

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/noodle/src'))

from noodle_agent.config import Settings
from noodle_agent.next.concepts.maintenance_bundle import enqueue, rollback
from noodle_agent.next.concepts.replay_promotion import POLICY, prepare, transfer_structure_coverage
from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.eligibility import eligible
from noodle_agent.next.navigation import frozen_candidate
from noodle_agent.next.pipeline_state import indexed_revisions
from noodle_agent.next.provider_limits import call_guard
from noodle_agent.next.repository import Conflict, one
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store


def execute(args):
    run=args.run.resolve()
    if not run.is_relative_to(ROOT/'.local-only'):
        raise ValueError('Keep promotion evidence inside .local-only')
    run.mkdir(parents=True,exist_ok=True)
    target=Store(args.target.resolve())
    source=Store(args.source.resolve())
    def write(name,value):
        (run/name).write_text(canonical(value),encoding='utf8')
    def forbid(request):
        raise Conflict('Accepted package must not regenerate prose: '+request['operation'])
    token=call_guard.set(forbid)
    try:
        if args.phase=='prepare':
            if (run/'bundle.json').exists():
                raise Conflict('Do not overwrite a prepared acceptance bundle')
            report_path=args.source.resolve().parent/'report.json'
            report=json.loads(report_path.read_text(encoding='utf8'))
            acceptance=json.loads(args.acceptance.read_text(encoding='utf8'))
            if not report.get('passed'):
                raise Conflict('Cold replay engineering acceptance has not passed')
            acceptance={**acceptance,'engineering_evidence_sha256':sha(report_path.read_bytes())}
            manifest=json.loads((args.source.resolve().parent/'input-manifest.json').read_text(encoding='utf8'))
            ids=[n['note_id'] for s in manifest['sources'] for n in s['atomics']]
            bundle=prepare(source,target,acceptance=acceptance,atomic_scope=ids)
            write('bundle.json',bundle)
            result={'bundle_sha256':bundle['bundle_sha256'],'publications':len(bundle['publications']),
                'retired':len(bundle['retired']),'mapped_identities':len(bundle['identity_rebase']['mapping']),
                'scope_atomics':len(bundle['atomics'])}
            write('prepared.json',result)
            return result
        bundle=json.loads((run/'bundle.json').read_text(encoding='utf8'))
        if args.phase in {'index','rollback'} and not (run/'receipt.json').exists():
            raise Conflict('Wait for the native apply receipt before indexing or rollback')
        if bundle.get('identity_rebase',{}).get('policy')!=POLICY:
            raise Conflict('Retain the old bundle and prepare a new package with the current identity mapping policy')
        settings=replace(Settings.from_env(ROOT),llm_api_key=None,vision_api_key=None,mineru_api_token=None)
        if args.phase=='apply':
            if (run/'rollback.json').exists():
                raise Conflict('A rolled-back rehearsal is immutable; prepare a separate package for another target')
            job=enqueue(target,bundle)
            app=Application(target,settings=settings,index_mode='KEYWORD_ONLY',job_kinds=['APPLY_CONCEPT_MAINTENANCE'])
            step=app.run_once('accepted-workspace-promotion')
            if step is None:
                with target.db.engine.connect() as c:
                    row=one(c,'SELECT status,result_json FROM jobs WHERE id=:j',j=job)
                step={'status':row['status'],'result':json.loads(row['result_json'])}
            if step['status']!='SUCCEEDED':
                write('failure.json',step)
                raise Conflict('Native package publication failed; retained receipt')
            write('receipt.json',step['result'])
            return {'status':'APPLIED','published':len(step['result']['published']),'retired':len(step['result']['retired'])}
        if args.phase=='rollback':
            if not target.root.is_relative_to(ROOT/'.local-only'):
                raise Conflict('This acceptance command only rehearses rollback in an isolated copy')
            result=rollback(target,bundle,json.loads((run/'receipt.json').read_text(encoding='utf8')))
            write('rollback.json',result)
            return result
        if args.phase in {'index','restore-index'}:
            if args.phase=='restore-index' and not (run/'rollback.json').exists():
                raise Conflict('Restore indexing requires a completed rollback receipt')
            for path in source.path('vectors/cache').rglob('*.npy'):
                ref=path.relative_to(source.root).as_posix()
                digest=source.path(ref+'.sha256').read_text(encoding='utf8')
                data=source.read(ref,digest)
                if not target.path(ref).exists():
                    target.put(ref,data)
                if sha(target.path(ref).read_bytes())==digest:
                    if not target.path(ref+'.sha256').exists():
                        target.put(ref+'.sha256',digest.encode())
                elif target.path(ref+'.sha256').exists():
                    target.read(ref,target.path(ref+'.sha256').read_text(encoding='utf8'))
                else:
                    raise Conflict('Existing vector cache has no valid checksum receipt')
            for kinds in [('RECONCILE_DEPENDENCIES',),('UPDATE_MAPS',),('INDEX_NOTE',),('BUILD_INDEX',)]:
                app=Application(target,settings=settings,job_kinds=kinds)
                while step:=app.run_once('accepted-workspace-index'):
                    if step['status']!='SUCCEEDED':
                        write('index-failure.json',step)
                        raise Conflict('Accepted transfer indexing failed')
            for item in bundle['publications'] if args.phase=='index' else []:
                current=target.get_note(item['note']['id'])
                accepted=item['proof']['candidate']
                if frozen_candidate(target,current['revision']['id']).model_dump()!=accepted:
                    raise Conflict('Published result differs from the rebased checked candidate')
            with target.db.engine.connect() as c:
                current=eligible(c)
            indexed,_=indexed_revisions(target,current)
            result={'passed':len(indexed)==len(current),'indexed':len(indexed),'effective':len(current),
                'concepts':sum(n['kind']=='CONCEPT' for n in current),'semantic_regeneration_calls':0}
            if args.phase=='index' and result['passed']:
                result['structure_coverage']=transfer_structure_coverage(source,target,bundle)
            write('index-acceptance.json' if args.phase=='index' else 'rollback-index-acceptance.json',result)
            return result
        raise ValueError('Unknown promotion phase')
    finally:
        call_guard.reset(token)
        source.close()
        target.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['prepare','apply','index','rollback','restore-index'])
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--acceptance',type=Path)
    args=parser.parse_args()
    if args.phase=='prepare' and not args.acceptance:
        parser.error('Prepare requires an explicit recorded acceptance file')
    print(json.dumps(execute(args),ensure_ascii=False))
