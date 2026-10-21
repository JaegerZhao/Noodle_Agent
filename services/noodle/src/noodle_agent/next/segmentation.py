"""Deterministic codepoint ranges preserving math, tables and adjacent definitions."""

import re
from functools import lru_cache

from .planning import QualityFailure


@lru_cache(maxsize=1)
def encoding():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def tokens(text):
    return len(encoding().encode(text, disallowed_special=()))


def ranges(text, target=400, maximum=8000):
    blocks = []
    start = 0
    position = 0
    fence = False
    math = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fence = not fence
        if len(re.findall(r"(?<!\\)\$\$", line)) % 2:
            math = not math
        position += len(line)
        if not stripped and not fence and not math:
            if text[start:position].strip():
                blocks.append((start, position))
            start = position
    if start < len(text):
        blocks.append((start, len(text)))
    if not blocks and text.strip():
        blocks = [(0, len(text))]
    # A displayed formula travels with both the preceding explanation and following definitions.
    groups = []
    i = 0
    while i < len(blocks):
        a, b = blocks[i]
        if "$$" in text[a:b]:
            if groups:
                a = groups.pop()[0]
            if i + 1 < len(blocks):
                i += 1
                b = blocks[i][1]
        groups.append((a, b))
        i += 1
    result = []
    current = None
    for a, b in groups:
        if tokens(text[a:b]) > maximum:
            raise QualityFailure(
                "A semantic block exceeds the model budget; no content was truncated"
            )
        if current and tokens(text[current[0] : b]) <= target:
            current = (current[0], b)
        else:
            if current:
                result.append(current)
            current = (a, b)
    if current:
        result.append(current)
    return result
