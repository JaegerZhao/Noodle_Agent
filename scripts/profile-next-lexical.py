"""Compare FTS query plans read-only; never call suppliers or alter a library."""
import argparse
import json
import sqlite3
from pathlib import Path
from time import perf_counter

from noodle_agent.next.retrieval.lexical import match_query


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sql = ('SELECT chunk_id,bm25(knowledge_fts,0,0,1.5,1.0) score FROM knowledge_fts '
           'WHERE knowledge_fts MATCH :q AND generation=:g '
           'AND chunk_id IN (SELECT value FROM json_each(:ids)) ')
    order = 'ORDER BY score,chunk_id LIMIT :limit'
    cases = [
        'SVM 的硬间隔与软间隔有什么区别？松弛变量和 C 的作用是什么？',
        '根据这篇论文，为什么缩放点积注意力要除以 sqrt(d_k)？只解释原因，并给出原始页码。',
        '紫色水母QZ-739定理是什么？',
    ]
    report = []
    with sqlite3.connect((args.library.resolve()/'noodle.db').as_uri()+'?mode=ro', uri=True) as c:
        g = c.execute("SELECT generation FROM index_heads WHERE mode_key LIKE 'HYBRID:%' AND backend='exact'").fetchone()[0]
        start = perf_counter()
        lo, hi, count = c.execute('SELECT min(rowid),max(rowid),count(*) FROM knowledge_fts WHERE generation=?', (g,)).fetchone()
        bounds_ms = (perf_counter()-start)*1000
        ids = [r[0] for r in c.execute('SELECT chunk_id FROM knowledge_fts WHERE generation=?', (g,))]
        for question in cases:
            params = {'q':match_query(question),'g':g,'ids':json.dumps(ids),'lo':lo,'hi':hi,'limit':50}
            item = {'question':question,'generation':g,'rows':count,'bounds_ms':bounds_ms,'variants':{}}
            for name, statement in [('original',sql+order),('rowid_bounds',sql+'AND rowid>=:lo AND rowid<=:hi '+order)]:
                start = perf_counter()
                result = c.execute(statement,params).fetchall()
                item['variants'][name] = {'ms':(perf_counter()-start)*1000,'rows':result,'plan':c.execute('EXPLAIN QUERY PLAN '+statement,params).fetchall()}
                print(json.dumps({'case':question,'variant':name,'ms':item['variants'][name]['ms']},ensure_ascii=False),flush=True)
            item['same_results'] = item['variants']['original']['rows'] == item['variants']['rowid_bounds']['rows']
            report.append(item)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'same_results':all(i['same_results'] for i in report),'output':str(args.output)}))


if __name__ == '__main__':
    main()
