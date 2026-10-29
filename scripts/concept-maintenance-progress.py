"""Read-only progress for a prepared incremental Concept run."""

import argparse
import json
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('run', type=Path)
args = parser.parse_args()
run = args.run.resolve()
baseline = json.loads((run / 'baseline.json').read_text(encoding='utf-8'))['baseline']
with sqlite3.connect((run / 'library/noodle.db').as_uri() + '?mode=ro', uri=True) as c:
    c.row_factory = sqlite3.Row
    jobs = [dict(r) for r in c.execute('SELECT kind,status,count(*) count FROM jobs WHERE rowid>? GROUP BY kind,status', (baseline['job_rowid'],))]
    calls = [dict(r) for r in c.execute('SELECT kind,operation,model,status,input_tokens,output_tokens,elapsed_ms FROM provider_calls WHERE rowid>? ORDER BY rowid DESC LIMIT 5', (baseline['provider_rowid'],))]
result = {'jobs': jobs, 'latest_calls': calls}
if (run / 'call-budget.json').exists():
    budget = json.loads((run / 'call-budget.json').read_text(encoding='utf-8'))
    result['model_calls_started'] = budget['calls']
    result['estimated_input_tokens'] = budget['estimated_input_tokens']
if (run / 'apply-results.json').exists():
    result['result_status'] = json.loads((run / 'apply-results.json').read_text(encoding='utf-8'))['status']
print(json.dumps(result, ensure_ascii=False, indent=2))
