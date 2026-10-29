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
    # Question framing is not the subject. In particular 什么是熵 must retain
    # the single-character subject 熵, rather than only bigrams 是熵 / 什么.
    text = re.sub(r'^(?:(?:请(?:帮我)?(?:解释|介绍|说明|简述)(?:一下)?|什么是|何谓)\s*)+', '', text.strip())
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms(text))
