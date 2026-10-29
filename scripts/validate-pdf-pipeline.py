"""Observe a PDF through the production API; dry-run unless --execute.

Server owns verified MinerU reuse and planning. No copied plans, manual content
changes, repair retries or changes to running task state.
"""
import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def observe(client, revision, *, source_id=None, pause=time.sleep, interval=2, record=lambda _: None):
    while True:
        response = client.get('/internal/next/generation-tasks', params={'limit': 100})
        response.raise_for_status()
        candidates = [j for j in response.json()['items'] if j['source_revision_id'] == revision]
        if candidates:
            job = max(candidates, key=lambda j: (j['created_at'], j['id']))
            response = client.get(f"/internal/next/jobs/{job['id']}/timeline")
            response.raise_for_status()
            value = response.json()
            record(value)
            background = (value.get('readiness') or {}).get('concept_maintenance', {}).get('status')
            if job['status'] in {'REVIEW', 'PAUSED', 'CANCELLED', 'FAILED'} or (
                    job['status'] == 'SUCCEEDED' and background in {'COMPLETE', 'PARTIAL', 'PAUSED', None}):
                return value
        else:
            response = client.get('/internal/next/jobs')
            response.raise_for_status()
            blocked = [j for j in response.json() if source_id and j.get('entity_id') == source_id
                       and j['kind'] == 'PARSE_SOURCE' and j['status'] in {'REVIEW', 'PAUSED', 'FAILED'}]
            if blocked:
                value = {'status': 'UPSTREAM_BLOCKED', 'jobs': blocked}
                record(value)
                return value
        pause(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--base-url', default='http://127.0.0.1:8011')
    parser.add_argument('--execute', action='store_true', help='Upload through production; configured providers may charge')
    parser.add_argument('--regenerate-removed', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--poll-seconds', type=float, default=2)
    args = parser.parse_args()
    if args.poll_seconds < 1:
        parser.error('--poll-seconds must be at least 1')
    content = args.pdf.read_bytes()
    if not content.startswith(b'%PDF-'):
        parser.error('Input is not a PDF')
    manifest = {'pdf': args.pdf.name, 'sha256': hashlib.sha256(content).hexdigest(),
                'started_at': datetime.now(UTC).isoformat(), 'execute': args.execute}
    if not args.execute:
        print(json.dumps({**manifest, 'status': 'DRY_RUN'}, ensure_ascii=False))
        return
    output = args.output or Path('.local-only') / ('pdf-api-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ'))
    output.mkdir(parents=True, exist_ok=False)
    def save(name, value):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    save('manifest.json', manifest)
    with httpx.Client(base_url=args.base_url.rstrip('/'), timeout=120, trust_env=False) as client:
        response = client.post('/internal/next/sources', params={'regenerate_removed': args.regenerate_removed},
                               files={'file': (args.pdf.name, content, 'application/pdf')})
        response.raise_for_status()
        accepted = response.json()
        save('accepted.json', accepted)
        try:
            result = observe(client, accepted['revision_id'], source_id=accepted['source_id'], interval=args.poll_seconds,
                             record=lambda value: save('timeline.json', value))
            save('result.json', result)
        except KeyboardInterrupt:
            save('observer-stopped.json', {'at': datetime.now(UTC).isoformat(), 'generation_unchanged': True})
            raise
    print(json.dumps({'output': str(output.resolve()), 'status': result.get('job', {}).get('status', result.get('status'))}))


if __name__ == '__main__':
    main()
