"""Private immutable provider telemetry without depending on the legacy database."""

from datetime import datetime

from .contracts import canonical, sha
from .jobs import require_lease
from .repository import identifier, insert, now


def prior_truncated_response(repository, lease, *, system, user, operation, **_):
    """Replay a known failed request as a failure, never as accepted content.

    Also recover older requests with telemetry but no generation error receipt.
    Exact request hashes exclude other reviews and changed repair requests.
    """
    import json

    from ..adapters.knowledge.common import TruncatedStructuredResponse
    from .repository import rows

    digest = sha(system + '\n' + json.dumps(user, ensure_ascii=False))
    with repository.db.engine.connect() as c:
        records = rows(c, "SELECT storage_ref,sha256 FROM build_artifacts WHERE job_id=:job AND kind='LLM_CALL' AND input_sha256=:digest", job=lease.job_id, digest=digest)
    for record in records:
        call = json.loads(repository.read(record['storage_ref'], record['sha256']))
        if call.get('operation') == operation and call.get('status') == 'FAILED' and call.get('finish_reason') in {'length', 'max_tokens'}:
            raise TruncatedStructuredResponse('Known truncated structured response: ' + record['storage_ref'])


def recover_format_response(repository, lease, *, system, user, operation, model):
    """Reuse an exact complete failed JSON response after a transport-only fix."""
    import json

    from ..adapters.knowledge.common import _loads_structured_json
    from .repository import rows

    digest = sha(system + '\n' + json.dumps(user, ensure_ascii=False))
    with repository.db.engine.connect() as c:
        records = rows(c, "SELECT storage_ref,sha256 FROM build_artifacts WHERE job_id=:j AND kind='LLM_CALL' AND input_sha256=:h ORDER BY created_at DESC", j=lease.job_id, h=digest)
    for record in records:
        call = json.loads(repository.read(record['storage_ref'], record['sha256']))
        if (call.get('operation') != operation or call.get('model') != model or call.get('status') != 'FAILED'
                or call.get('finish_reason') != 'stop' or not call.get('output_ref')
                or 'Invalid \\escape' not in str(call.get('error', ''))):
            continue
        raw = repository.read(call['output_ref'], call['output_sha256']).decode('utf-8')
        try:
            decoded = _loads_structured_json(raw)
        except (ValueError, TypeError):
            continue
        receipt = {'policy':'json-slash-recovery-v1','job_id':lease.job_id,'operation':operation,
            'input_sha256':digest,'original_ref':call['output_ref'],'original_sha256':call['output_sha256'],
            'decoded_sha256':sha(canonical(decoded)), 'provider_call_ref':record['storage_ref'], 'network_call':False}
        data = canonical(receipt).encode('utf-8')
        repository.put(f'artifacts/format-recoveries/{sha(data)}.json', data)
        return decoded
    return None


def recover_vision_format_response(repository, lease, *, input_text, model, prompt_version, image_sha256):
    """Replay an exact complete failed OCR table after a wire-format fix."""
    import json

    from ..adapters.knowledge.common import _loads_structured_json
    from .repository import rows

    digest = sha(input_text)
    with repository.db.engine.connect() as c:
        records = rows(c, "SELECT storage_ref,sha256 FROM build_artifacts WHERE job_id=:j AND kind='VISION_CALL' AND input_sha256=:h ORDER BY created_at DESC", j=lease.job_id, h=digest)
    for record in records:
        call = json.loads(repository.read(record['storage_ref'], record['sha256']))
        if (call.get('status') != 'FAILED' or call.get('model') != model or call.get('prompt_version') != prompt_version
                or call.get('image_sha256') != image_sha256 or call.get('error_code') != 'ValidationError'
                or not call.get('output_ref')):
            continue
        raw = repository.read(call['output_ref'], call['output_sha256']).decode('utf-8')
        try:
            result = _loads_structured_json(raw)
        except (ValueError, TypeError):
            continue
        images = result.get('images', [])
        if not (isinstance(images, list) and len(images) == 1 and isinstance(images[0], dict)):
            continue
        ocr = images[0].get('ocr_text')
        if not (isinstance(ocr, list) and ocr and all(isinstance(row, list) and row and all(isinstance(cell, str) for cell in row) for row in ocr)):
            continue
        receipt = {'policy':'vision-table-wire-recovery-v1', 'job_id':lease.job_id,
            'original_ref':call['output_ref'], 'original_sha256':call['output_sha256'],
            'provider_call_ref':record['storage_ref'], 'input_sha256':digest,
            'image_sha256':image_sha256, 'network_call':False}
        data = canonical(receipt).encode('utf-8')
        repository.put(f'artifacts/format-recoveries/{sha(data)}.json', data)
        return raw
    return None


class ProviderAudit:
    def __init__(self, repository, lease):
        self.repository, self.lease = repository, lease

    def record(self, kind, **values):
        from .provider_limits import trace
        values = {**(trace.get() or {}), **values}
        if hasattr(values.get("usage"), "model_dump"):
            values["usage"] = values["usage"].model_dump()
        values = {
            key: (
                value.isoformat()
                if isinstance(value, datetime)
                else str(value)
                if isinstance(value, Exception)
                else value
            )
            for key, value in values.items()
        }
        # The legacy transport supplies full input text. Private source snippets are
        # already in checkpoints; telemetry retains hashes and usage, not another body.
        for field in ("input_text", "output_text"):
            value = values.pop(field, None)
            if value is not None:
                values[field.removesuffix("_text") + "_sha256"] = sha(value)
                if field == "output_text" and values.get("status") == "FAILED":
                    output_ref = f"artifacts/failed-responses/{sha(value)}.txt"
                    self.repository.put(output_ref, value.encode("utf-8"))
                    values["output_ref"] = output_ref
        data = canonical(values).encode("utf-8")
        ref = f"artifacts/calls/{identifier()}.json"
        self.repository.put(ref, data)
        with self.repository.transaction() as c:
            require_lease(c, self.lease)
            from .metrics import project_call

            project_call(c, self.lease.job_id, kind, values, ref)
            insert(
                c,
                "build_artifacts",
                id=identifier(),
                job_id=self.lease.job_id,
                kind=kind,
                storage_ref=ref,
                sha256=sha(data),
                policy_version=self.repository.policy_version,
                input_sha256=values.get("input_sha256", sha("")),
                created_at=now(),
            )

    def record_llm(self, **values):
        self.record("LLM_CALL", **values)

    def record_vision(self, **values):
        self.record("VISION_CALL", **values)

    def record_embedding(self, **values):
        self.record("EMBEDDING_CALL", **values)


class RequestAudit:
    """API calls have request receipts, not fabricated background job leases."""

    def __init__(self, repository, trace=None):
        self.repository = repository
        self.request_id = identifier()
        self.trace = {k: str(v)[:200] for k, v in (trace or {}).items() if v}

    def record(self, kind, values):
        values = {**values, **self.trace, 'request_id': self.request_id, 'kind': kind, 'recorded_at': now()}
        if hasattr(values.get('usage'), 'model_dump'):
            values['usage'] = values['usage'].model_dump()
        for field in ('input_text', 'output_text'):
            if values.get(field) is not None:
                values[field.removesuffix('_text') + '_sha256'] = sha(str(values.pop(field)))
        values = {k: v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, Exception) else v for k, v in values.items()}
        self.repository.put(f'artifacts/api-calls/{self.request_id}/{identifier()}.json', canonical(values).encode())

    def record_llm(self, **values):
        self.record('LLM_CALL', values)

    def record_embedding(self, **values):
        self.record('EMBEDDING_CALL', values)
