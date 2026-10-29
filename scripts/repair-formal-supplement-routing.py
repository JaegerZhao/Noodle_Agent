"""Request the bounded, authorized SVM supplementary object repair; never edit prose."""

import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/noodle/src'))

from noodle_agent.next.concepts.maintenance import request_replan
from noodle_agent.next.contracts import canonical
from noodle_agent.next.repository import Conflict, one, rows
from noodle_agent.next.store import Store

LIBRARY=ROOT/'data/knowledge-formal-20261025T043825Z'
RUN=ROOT/'.local-only/concept-supplement-validation-20261027'
SOURCE_HASH='a8faaede4bf4da5a952f14c387c609810ca0c6395cde7524f6f13a38acc423d8'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['route','organize','atomic'])
    parser.add_argument('--revision',choices=['initial','coverage-priority','foundation-canonical','retire-fragmented-variants'],default='initial')
    args=parser.parse_args()
    store=Store(LIBRARY)
    try:
        with store.db.engine.connect() as c:
            source=one(c,'SELECT r.id FROM source_revisions r JOIN sources s ON s.current_revision_id=r.id WHERE r.sha256=:h AND s.deleted_at IS NULL',h=SOURCE_HASH)
            if not source:
                raise Conflict('The exact authorized slides are not current')
            job=one(c,"SELECT status,result_json FROM jobs WHERE kind='GENERATE_ATOMICS' AND json_extract(input_json,'$.revision_id')=:r ORDER BY created_at DESC LIMIT 1",r=source['id'])
            if not job or job['status']!='SUCCEEDED' or json.loads(job['result_json']).get('coverage',{}).get('failed_plans'):
                raise Conflict('Complete the Atomic recovery before rerouting')
            atomics=rows(c,"SELECT n.id,n.current_revision_id FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='ATOMIC' AND n.visibility='ACTIVE' AND r.status='PUBLISHED' AND n.creation_key LIKE :p ORDER BY n.id",p=f"atomic:{source['id']}:%")
            if len(atomics)!=2:
                raise Conflict('This recovery was accepted for exactly the two current slide Atomics')
            before_calls=c.exec_driver_sql('SELECT max(rowid) FROM provider_calls').scalar_one()
        if args.phase=='atomic':
            from noodle_agent.next.contracts import sha
            from noodle_agent.next.navigation import frozen_candidate
            chosen=[]
            for atomic in atomics:
                candidate=frozen_candidate(store,atomic['current_revision_id'])
                findings=[{'section_id':s.section_id,
                    'issue':'该节照录了来源中一阶项减半个二次项的对偶目标，却标为min，并把它当作原始问题等价的可执行优化方向。该目标是应最大化的拉格朗日对偶函数；来源第22/37页也写min。请对照本节有效来源和前面的推导，明确说明来源记号与对偶优化方向的矛盾，不能无说明复制或无说明改写来源。保留其他定义、图和推导。'}
                    for s in candidate.sections if r'\min_{\alpha}' in s.markdown]
                if findings:
                    chosen.append((atomic,findings))
            if len(chosen)!=1:
                raise Conflict('Expected exactly one located slide Atomic with the observed dual-direction issue')
            atomic,findings=chosen[0]
            with store.repository.transaction() as c:
                value={'event_id':store.repository.event(c,'ATOMIC_REPAIR_REQUESTED',atomic['id'],atomic['current_revision_id'],
                    {'expected_revision':atomic['current_revision_id'],'findings':findings,
                     'cause':'formal-supplement-readback-20261027'},
                    key='atomic-quality:'+atomic['current_revision_id']+':'+sha(canonical(findings))),
                    'findings':findings}
        elif args.phase=='route':
            context=None
            if args.revision=='retire-fragmented-variants':
                context={'kind':'CONSOLIDATE_EXISTING_INTERNAL_VARIANTS',
                    'excluded_candidate_ids':['34f50e63-3320-428e-b81c-2e79d8b48360'],
                    'preferred_foundation_id':'ca57510b-6cfb-4891-b66d-fc9ac7c6cae6',
                    'reason':'用户要求SVM为统一对象，教材SVM现有有效核心已包含线性、软间隔与核技巧；课件的核SVM仅为内部形式的直观介绍。本次明确复核旧分裂并迁移支持，排除待退出窄对象以免按旧标题继续固化分散，不删除历史或改写Atomic。'}
            value=request_replan(store,[a['current_revision_id'] for a in atomics],
                'slides-supplement-object-repair-20261027:'+args.revision,resolution_context=context)
        else:
            with store.repository.transaction() as c:
                targets=sorted({r['concept_id'] for r in rows(c,"SELECT d.concept_id FROM contribution_decisions d JOIN note_revisions r ON r.id=d.atomic_revision_id JOIN notes n ON n.current_revision_id=r.id WHERE d.status='ACCEPTED' AND d.concept_id IS NOT NULL AND n.creation_key LIKE :p",p=f"atomic:{source['id']}:%")})
                epoch=c.exec_driver_sql('SELECT validity_epoch FROM storage_contract WHERE id=1').scalar_one()
                value={'event_id':store.repository.event(c,'ORGANIZATION_REVIEW_REQUESTED',
                    'concept-organization',None,{'source_revision':source['id'],'concept_ids':targets},
                    key='slides-object-repair-organization:'+str(epoch)),'concept_ids':targets}
        receipt={**value,'phase':args.phase,'atomics':[dict(a) for a in atomics],
            'provider_rowid_before':before_calls,'source_revision_id':source['id'],
            'reason':'Re-evaluate variants against actual checked legacy coverage; keep current Atomic prose and original Concept UUIDs where appropriate'}
        destination=RUN/('routing-repair-'+args.phase+'-'+args.revision+'.json')
        if not destination.exists():
            destination.write_text(canonical(receipt),encoding='utf-8')
        print(json.dumps(receipt,ensure_ascii=False,indent=2))
    finally:
        store.close()


if __name__=='__main__':
    main()
