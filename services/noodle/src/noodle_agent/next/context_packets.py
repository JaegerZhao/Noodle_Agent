"""Stage-specific views; the immutable source remains available for exact checks."""


def units_packet(units):
    result = []
    for unit in units:
        u = unit.model_dump() if hasattr(unit, "model_dump") else unit
        result.append(
            {
                k: u[k]
                for k in ("unit_id", "block_id", "page", "kind", "text", "asset_ref")
                if u.get(k) is not None
            }
        )
    return result


def judge_packet(inputs, draft):
    return {
        "plan": {k: inputs["plan"][k] for k in ("title", "question", "scope")},
        "source_contract": inputs["source_contract"],
        "units": inputs["units"],
        "draft": draft.model_dump(),
        "image_paths": inputs["image_paths"],
        "page_contexts": inputs["page_contexts"],
        "evidence_precedence": inputs["evidence_precedence"],
    }


def repair_packet(inputs, draft, issues):
    matched = [
        s for s in draft.sections if any(s.key in issue or s.title in issue for issue in issues)
    ]
    selected = matched or draft.sections
    ids = {uid for s in selected for uid in s.units}
    return {
        "scope": inputs["plan"]["scope"],
        "source_contract": inputs["source_contract"],
        "issues": issues,
        "sections": [s.model_dump() for s in selected],
        "units": [u for u in inputs["units"] if u["unit_id"] in ids],
        "image_paths": inputs["image_paths"],
        "page_contexts": inputs["page_contexts"],
    }


def apply_repair(draft, repair, allowed):
    patches = {s.key: s.markdown for s in repair.sections}
    if len(patches) != len(repair.sections) or not set(patches) <= set(allowed):
        raise ValueError("Repair names unknown or repeated sections")
    return draft.model_copy(
        update={
            "sections": [
                s.model_copy(update={"markdown": patches[s.key]}) if s.key in patches else s
                for s in draft.sections
            ],
            "summary": repair.summary if repair.summary is not None else draft.summary,
        }
    )
