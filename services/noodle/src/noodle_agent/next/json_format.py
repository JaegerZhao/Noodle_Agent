"""Recover only a missing outer object brace; never repair or generate values."""

import json


def empty_diagnostic_envelope(raw, contract):
    """Compile redundant empty diagnostics; never discard content or actual findings."""
    if not isinstance(raw, dict):
        return raw, []
    allowed = contract.model_fields
    removable = [key for key in ('findings', 'issues') if key not in allowed and key in raw and raw[key] in (None, {}, [])]
    result = {k:v for k,v in raw.items() if k not in removable}
    normalizations = [{'code':'EMPTY_DIAGNOSTIC_ENVELOPE','field':k} for k in removable]
    for key, field in allowed.items():
        child = field.annotation
        if key in result and isinstance(result[key], dict) and hasattr(child, 'model_fields'):
            result[key], nested = empty_diagnostic_envelope(result[key], child)
            normalizations.extend({**item, 'field':key+'.'+item['field']} for item in nested)
    return result, normalizations


def close_outer_object(content, finish_reason):
    if finish_reason != "stop":
        return None
    text = content.strip()
    try:
        json.loads(text)
        return None
    except json.JSONDecodeError as exc:
        if exc.pos != len(text):
            return None
    if not text.startswith("{") or not text.endswith("}"):
        return None
    stack, string, escape = [], False, False
    for char in text:
        if string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                string = False
        elif char == '"':
            string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and (not stack or stack.pop() != ("{" if char == "}" else "[")):
            return None
    if string or escape or stack != ["{"]:
        return None
    normalized = text + "}"
    try:
        result = json.loads(normalized)
    except json.JSONDecodeError:
        return None
    return (result, normalized) if isinstance(result, dict) else None
