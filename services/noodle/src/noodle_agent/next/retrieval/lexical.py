"""Deterministic Chinese n-grams, Latin terms and formula tokens for FTS5."""

import re
import unicodedata


def terms(text):
    text = unicodedata.normalize("NFKC", text).lower()
    result = re.findall(r"[a-z][a-z0-9_]*|[0-9]+|[α-ω]", text)
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend(run if len(run) == 1 else (run[i : i + 2] for i in range(len(run) - 1)))
    return list(dict.fromkeys(result))


def searchable(text):
    return " ".join(terms(text))


def match_query(text):
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms(text))
