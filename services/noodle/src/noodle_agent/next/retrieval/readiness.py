"""Cheap runtime diagnostics: never call a model or disclose credentials."""

import os


def readiness(application):
    indexer = application.indexer
    modes = {}
    for mode in ('HYBRID', 'KEYWORD_ONLY'):
        if mode == 'HYBRID' and indexer.profile is None:
            modes[mode] = {'status': 'PROFILE_MISMATCH', 'message': '查询向量模型未配置。'}
            continue
        head = indexer.head(mode)
        modes[mode] = {'status': 'READY' if head else 'INDEX_PENDING',
                       'index_generation': head['id'] if head else None,
                       **({'lexical_projection_of': 'HYBRID'} if head and mode == 'KEYWORD_ONLY' and head['mode_key'].startswith('HYBRID:') else {})}
    settings = application.settings
    return {
        'status': modes[application.index_mode]['status'],
        'library_id': application.store.root.name,
        'code_generation': os.getenv('NOODLE_CODE_GENERATION'),
        'answer_model_configured': application.calls is not None,
        'embedding': {'configured': indexer.profile is not None,
                      'profile': {k: indexer.profile[k] for k in ('id', 'model', 'dimensions')} if indexer.profile else None,
                      'model': getattr(settings, 'embedding_model', None)},
        'backend': indexer.backend.name,
        'modes': modes,
        'rerank_configured': application.searcher.reranker is not None,
        'tools_contract': 'noodle-agent-v2',
    }
