"""Derive note links from immutable Markdown, ignoring code and math spans."""

import json
import posixpath
import re
from urllib.parse import unquote

from .repository import Conflict


def references(body):
    protected = re.sub(
        r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`|\$\$[\s\S]*?\$\$|(?<!\\)\$(?![\s$])[^$\n]+\$",
        lambda m: " " * len(m[0]),
        body,
    )
    result = []
    for match in re.finditer(r"\[\[([^\]\n]+)\]\]", protected):
        target, _, label = match[1].partition("|")
        result.append(
            {
                "target": target.strip(),
                "label": label or target,
                "start": match.start(),
                "end": match.end(),
                "wiki": True,
            }
        )
    for match in re.finditer(r"(?<!!)\[[^\]]+\]\(([^)]+\.md(?:#[^)]*)?)\)", protected):
        result.append(
            {"target": match[1], "start": match.start(), "end": match.end(), "wiki": False}
        )
    return sorted(result, key=lambda r: r["start"])


def resolve_reference(target, owner, notes):
    value = unquote(target).split("#", 1)[0].replace("\\", "/")
    path = posixpath.normpath(posixpath.join(posixpath.dirname(owner["stable_path"]), value))
    paths = {value, value + ".md", path, path + ".md"}
    exact = [n for n in notes if n["id"] == value or n["stable_path"] in paths]
    if len(exact) == 1:
        return exact[0]
    named = [
        n
        for n in notes
        if value.casefold()
        in {n["title"].casefold(), *(a.casefold() for a in json.loads(n["aliases_json"]))}
    ]
    local = [n for n in named if n["domain"] == owner["domain"]]
    matches = local or named
    if len(matches) != 1:
        raise Conflict("Link target is missing or ambiguous; select its stable path: " + value)
    return matches[0]
