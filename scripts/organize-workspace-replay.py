"""One explicit full-structure pass after the ordered sources have completed."""

import argparse
import json
import sys
import time
from pathlib import Path

from concept_replay_run import bootstrap, dump, report, upgrade_execution


def execute(run):
    if json.loads((run/'progress.json').read_text(encoding='utf8'))['status']!='COMPLETED':
        raise ValueError('Finish all ordered inputs before the global structure pass')
    upgrade_execution(run,'Full structure coverage must not reuse a neighborhood receipt; navigation counts use current Concept members')
    store,settings=bootstrap(run)
    from noodle_agent.next.organization import coverage, request_organization_review
    from noodle_agent.next.provider_limits import call_guard
    from noodle_agent.next.runtime import Application
    def scope(request):
        if not request['operation'].startswith('next.organization_'):
            raise ValueError('Final structure pass cannot rewrite Concept prose')
    token=call_guard.set(scope)
    try:
        request_organization_review(store,'ordered-replay-four-source-completion')
        steps=[]
        for kinds in [('REVIEW_ORGANIZATION','UPDATE_MAPS'),('INDEX_NOTE','BUILD_INDEX')]:
            app=Application(store,settings=settings,job_kinds=kinds)
            while True:
                step=app.run_once('workspace-global-structure')
                if step:
                    steps.append(step)
                    if step['status']=='REVIEW':
                        dump(run/'global-organization-failure.json',step)
                        raise ValueError('Global structure pass retained a failure receipt')
                    continue
                with store.db.engine.connect() as c:
                    pending=c.exec_driver_sql("SELECT count(*) FROM jobs WHERE status IN ('PENDING','RETRY','RUNNING') AND kind IN ("+','.join('?' for _ in kinds)+')',kinds).scalar_one()
                if not pending:
                    break
                time.sleep(2)
        value={'coverage':coverage(store),'steps':steps}
        dump(run/'global-organization.json',value)
        final=report(run,store)
        return {'passed':final['passed'],'coverage':value['coverage'],'steps':len(steps)}
    finally:
        call_guard.reset(token)
        store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    args=parser.parse_args()
    sys.stdout.reconfigure(encoding='utf8')
    print(json.dumps(execute(args.run.resolve()),ensure_ascii=False))
