"""Read back a native Concept promotion against its original full backup."""

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/noodle/src'))

from noodle_agent.next.contracts import canonical, sha


def main():
    run=ROOT/'.local-only/concept-workspace-20261028'
    baseline=json.loads((run/'formal-before/baseline.json').read_text(encoding='utf8'))['baseline']
    package=json.loads((run/'formal-promotion/bundle.json').read_text(encoding='utf8'))
    library=ROOT/'data/knowledge-formal-20261025T043825Z'
    c=sqlite3.connect('file:'+(library/'noodle.db').as_posix()+'?mode=ro',uri=True)
    c.row_factory=sqlite3.Row
    before=[n for n in baseline['heads'] if n['kind']=='ATOMIC']
    current={n['id']:dict(n) for n in c.execute('SELECT n.*,r.markdown_ref,r.markdown_sha256 FROM notes n LEFT JOIN note_revisions r ON r.id=n.current_revision_id')}
    unchanged=all(all(current[n['id']][k]==v for k,v in n.items()) for n in before)
    retained=all(current[target]['current_revision_id'] for target in package['identity_rebase']['mapping'].values())
    assets=set()
    for item in package['publications']:
        for section in item['proof']['candidate']['sections']:
            assets.update(re.findall(r'!\[[^\]]*\]\(((?:\.\./)?assets/[a-f0-9]{64}\.(?:png|jpg|jpeg|webp))\)',section['markdown']))
    for ref in assets:
        path=library/ref.removeprefix('../')
        if not path.resolve().is_relative_to(library.resolve()) or sha(path.read_bytes())!=path.name.split('.')[0]:
            raise RuntimeError('Published figure is missing or differs: '+ref)
    calls=c.execute('SELECT count(*) FROM provider_calls WHERE rowid>?',(baseline['provider_rowid'],)).fetchone()[0]
    backup=sqlite3.connect('file:'+(run/'formal-before/library/noodle.db').as_posix()+'?mode=ro',uri=True)
    original_events=list(backup.execute("SELECT * FROM job_stage_events WHERE job_id IN (SELECT id FROM jobs WHERE kind='GENERATE_ATOMICS') ORDER BY id"))
    current_events=[tuple(r) for r in c.execute("SELECT * FROM job_stage_events WHERE job_id IN (SELECT id FROM jobs WHERE kind='GENERATE_ATOMICS') ORDER BY id")]
    original_calls=list(backup.execute('SELECT * FROM provider_calls ORDER BY id'))
    current_calls=[tuple(r) for r in c.execute('SELECT * FROM provider_calls WHERE rowid<=? ORDER BY id',(baseline['provider_rowid'],))]
    backup.close()
    value={'passed':unchanged and retained and calls==0,'atomic_heads_preserved':len(before),'all_atomic_heads_unchanged':unchanged,
        'matched_concept_ids_preserved':len(package['identity_rebase']['mapping']),'checked_unique_figure_files':len(assets),
        'new_provider_calls_during_promotion':calls,'integrity':c.execute('PRAGMA integrity_check').fetchone()[0],
        'original_generation_events_unchanged':original_events==current_events,'original_call_receipts_unchanged':original_calls==current_calls,
        'foreign_key_violations':[list(r) for r in c.execute('PRAGMA foreign_key_check')],
        'indexed':json.loads((run/'formal-promotion/index-acceptance.json').read_text(encoding='utf8'))}
    value['passed']=value['passed'] and value['integrity']=='ok' and not value['foreign_key_violations'] and value['indexed']['passed'] and original_events==current_events and original_calls==current_calls
    (run/'formal-promotion/readback.json').write_text(canonical(value),encoding='utf8')
    c.close()
    print(json.dumps(value,ensure_ascii=False))
    if not value['passed']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
