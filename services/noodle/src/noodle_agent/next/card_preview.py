"""Derive a reading excerpt without images, generated footers or broken math cuts."""

import re
from functools import lru_cache
from pathlib import Path

from .contracts import sha
from .repository import InvalidKnowledge


def text_excerpt(markdown, fallback):
    body=markdown.split('<!-- noodle:derived -->',1)[0]
    body=re.sub(r'^ {0,3}#{1,6}[ \t]+[^\n]*(?:\n|$)', '',body,flags=re.MULTILINE)
    body=re.sub(r'!\[(?:\\.|[^\]\\])*\]\([^\n]*\)', '',body)
    body=re.sub(r'<img\b[^>]*>', '',body,flags=re.IGNORECASE)
    body=re.sub(r'```[^\n]*\n[\s\S]*?```|~~~[^\n]*\n[\s\S]*?~~~','',body)
    for paragraph in re.split(r'\n\s*\n',body):
        text=paragraph.strip()
        if text and not text.startswith(('#','```','~~~','$$','\\[','|','- ','* ')):
            # The frontend clamps visual lines; never slice through a formula.
            return text if len(text)<=1200 else fallback
    return fallback


@lru_cache(maxsize=2048)
def stored_excerpt(root,ref,digest,fallback):
    base=Path(root).resolve()
    path=(base/ref).resolve()
    if not path.is_relative_to(base):
        raise InvalidKnowledge('Card excerpt escaped the library')
    data=path.read_bytes()
    if sha(data)!=digest:
        raise InvalidKnowledge('Card excerpt content hash differs')
    return text_excerpt(data.decode(),fallback)
