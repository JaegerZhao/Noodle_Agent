"""Private immutable provider telemetry without depending on the legacy database."""

from datetime import datetime

from .contracts import canonical, sha
from .jobs import require_lease
from .repository import identifier, insert, now


class ProviderAudit:
    def __init__(self, repository, lease):
        self.repository, self.lease = repository, lease

    def record(self, kind, **values):
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
