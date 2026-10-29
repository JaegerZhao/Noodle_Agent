"""Supplier-reported usage only; keep Agent usage outside this RAG subtotal."""
import json


def request_usage(store, result):
    request_id = result.get('request_id', '')
    if not request_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in request_id):
        return {}
    groups = {}
    for path in store.path('artifacts/api-calls/' + request_id).glob('*.json'):
        call = json.loads(path.read_text(encoding='utf-8'))
        kind = {'LLM_CALL': 'llm', 'EMBEDDING_CALL': 'embedding'}.get(call.get('kind'))
        if not kind:
            continue
        entry = groups.setdefault(kind, {'calls': 0, 'models': [], 'reported_calls': 0})
        entry['calls'] += 1
        if call.get('model') and call['model'] not in entry['models']:
            entry['models'].append(call['model'])
        usage = call.get('usage') or {}
        if usage:
            entry['reported_calls'] += 1
        values = {'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
                  'total_tokens': usage.get('total_tokens'), 'cached_input_tokens': usage.get('prompt_cache_hit_tokens')}
        for key, value in values.items():
            if isinstance(value, int) and not isinstance(value, bool):
                entry[key] = entry.get(key, 0) + value
    diagnostics = result.get('diagnostics') or {}
    rerank = diagnostics.get('rerank') or {}
    if rerank.get('provider') == 'dashscope' and rerank.get('status') == 'READY':
        entry = {'calls': 1, 'models': [rerank['model']] if rerank.get('model') else [], 'reported_calls': 0}
        value = (rerank.get('usage') or {}).get('total_tokens')
        if isinstance(value, int) and not isinstance(value, bool):
            entry.update(total_tokens=value, reported_calls=1)
        groups['rerank'] = entry
    return groups
