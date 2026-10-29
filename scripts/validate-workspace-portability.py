"""Full real-library reading export and recovery, without any provider calls."""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/noodle/src'))

from noodle_agent.next.backup import recovery_zip, restore_package
from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.eligibility import eligible
from noodle_agent.next.pipeline_state import indexed_revisions
from noodle_agent.next.store import Store
from noodle_agent.next.vault import portable_files


def validate(library,run):
    if not run.is_relative_to(ROOT/'.local-only') or run.exists():
        raise ValueError('Use a new isolated acceptance directory')
    run.mkdir(parents=True)
    store=Store(library)
    started=time.monotonic()
    try:
        with store.db.engine.connect() as c:
            before=c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one()
        files=portable_files(store)
        workspace=files['.noodle-workspace.json']
        (run/'workspace.json').write_bytes(workspace)
        archive=recovery_zip(store)
        (run/'recovery.zip').write_bytes(archive)
        restored=restore_package(archive,run/'restored')
        copy=Store(run/'restored')
        try:
            exported=portable_files(copy)
            if files.keys()!=exported.keys() or any(sha(value)!=sha(exported[key]) for key,value in files.items()):
                raise ValueError('Recovered reading export differs')
            with copy.db.engine.connect() as c:
                current=eligible(c)
            indexed,_=indexed_revisions(copy,current)
            if len(indexed)!=len(current):
                raise ValueError('Recovered current index is incomplete')
        finally:
            copy.close()
        with store.db.engine.connect() as c:
            if c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one()!=before:
                raise ValueError('Portability unexpectedly called a provider')
        report={'passed':True,'reading_files':len(files),'workspace_sha256':sha(workspace),
            'archive_sha256':sha(archive),'archive_bytes':len(archive),'restore':restored,
            'indexed_current_revisions':len(indexed),'provider_calls':0,
            'elapsed_s':round(time.monotonic()-started,3)}
        (run/'acceptance.json').write_text(canonical(report),encoding='utf8')
        return report
    finally:
        store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library',type=Path)
    parser.add_argument('run',type=Path)
    args=parser.parse_args()
    print(json.dumps(validate(args.library.resolve(),args.run.resolve()),ensure_ascii=False))
