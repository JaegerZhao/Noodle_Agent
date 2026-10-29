"""Markdown-aware prose validation: code examples are content, not document headings."""

import re
from html.parser import HTMLParser

from .planning import QualityFailure


def contains_html(body):
    """Distinguish actual markup from prose comparisons spanning '<' and '>'."""
    class Detector(HTMLParser):
        found = False
        def handle_starttag(self, tag, attrs):
            self.found = True
        def handle_endtag(self, tag):
            self.found = True
        def handle_comment(self, data):
            self.found = True
        def handle_decl(self, decl):
            self.found = True
        def handle_pi(self, data):
            self.found = True
    parser = Detector(convert_charrefs=False)
    parser.feed(body)
    parser.close()
    return parser.found


def without_fences(body):
    result = []
    fence = None
    for line in body.splitlines(keepends=True):
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            marker, count = fence
            if re.fullmatch(r" {0,3}" + re.escape(marker) + "{" + str(count) + r",}\s*", line):
                fence = None
            result.append("\n" if line.endswith("\n") else " ")
        elif opening:
            marker = opening[1]
            if marker[0] == "`" and "`" in opening[2]:
                raise QualityFailure("Invalid code-fence info string")
            fence = (marker[0], len(marker))
            result.append("\n" if line.endswith("\n") else " ")
        else:
            result.append(line)
    if fence:
        raise QualityFailure("Unclosed Markdown code fence")
    return "".join(result)


def section_headings(body):
    """Demote model-produced article headings into a chapter, preserving code."""
    prose = without_fences(body)
    levels = [len(m[1]) for m in re.finditer(r"^(#{1,6})\s", prose, re.MULTILINE)]
    shift = max(0, 3 - min(levels)) if levels else 0
    if not shift:
        return body
    result = []
    for original, visible in zip(
        body.splitlines(keepends=True), prose.splitlines(keepends=True), strict=True
    ):
        if re.match(r"^#{1,6}\s", visible):
            original = re.sub(
                r"^(#{1,6})(?=\s)", lambda m: "#" * min(6, len(m[1]) + shift), original
            )
        result.append(original)
    return "".join(result)


def escape_literal_tokens(body):
    """Compile common NLP tokens as text, preserving code and math verbatim."""
    token = re.compile(r"<(?:/?(?:eos|bos|sos|unk|pad|cls|sep|mask)|[XY]|extra_id_\d+|\|[a-zA-Z_]+\|)>", re.IGNORECASE)
    protected = re.compile(r"(`+)[^`\n]*?\1|(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|(?<![\\$])\$(?!\$)[^$\n]+\$")
    prose = without_fences(body)
    pairs = list(zip(body.splitlines(keepends=True), prose.splitlines(keepends=True), strict=True))
    masked = ''.join(original if visible.strip() else ''.join('\n' if char == '\n' else ' ' for char in original) for original, visible in pairs)
    spans = [m.span() for m in protected.finditer(masked)]
    result = []
    position = 0
    for original, visible in pairs:
        start = position
        position += len(original)
        if not visible.strip():
            result.append(original)
            continue
        def literal(match, spans=spans, position=start):
            if any(left <= position + match.start() < right for left, right in spans):
                return match[0]
            return match[0].replace('<', '&lt;').replace('>', '&gt;')
        result.append(token.sub(literal, original))
    return ''.join(result)


def heading_key(title):
    title = re.sub(r'^\s*(?:\d+(?:\.\d+)*[.、]?\s*|第[一二三四五六七八九十\d]+[章节]\s*)', '', title)
    return re.sub(r'[^\w\u4e00-\u9fff]', '', title).casefold()


def normalize_math_code_spans(body):
    """Compile unambiguous TeX code spans; retain actual executable examples."""
    prose = without_fences(body)
    result = []
    for original, visible in zip(body.splitlines(keepends=True), prose.splitlines(keepends=True), strict=True):
        if not visible.strip():
            result.append(original)
            continue
        def replace(match):
            value = match[1]
            if value.startswith('$') and value.endswith('$') and value.count('$') in {2, 4}:
                return value
            if re.search(r'\\(?:frac|sum|alpha|beta|theta|hat|boldsymbol|mathbf|mathbb|operatorname)\b|[_^]\{', value) and not re.search(r'[\'";]', value):
                return '$' + value + '$'
            return match[0]
        result.append(re.sub(r'(?<![\\`])`(?!`)([^`\n]+)`(?!`)', replace, original))
    return ''.join(result)


def normalize_math_markup(body):
    """Repair unambiguous TeX escaping/extra closing groups inside math only."""
    commands = {'bar', 'breve', 'beta', 'begin', 'big', 'bigl', 'bigr', 'biggl', 'biggr',
                'binom', 'bmod', 'boldsymbol', 'bold', 'bm', 'bf', 'boxed', 'frac', 'forall'}
    pattern = re.compile(r'(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|(?<![\\$])\$(?![\s$])(?:\\.|[^$\n])+?(?<!\s)\$')
    def compile_math(match):
        text = match[0]
        def restore(control):
            command = ('b' if control[1] == '\x08' else 'f') + control[2]
            return '\\' + command if command in commands else control[0]
        text = re.sub(r'([\x08\x0c])([a-zA-Z]+)', restore, text)
        result, depth, i = [], 0, 0
        while i < len(text):
            char = text[i]
            if char == '\\' and i + 1 < len(text):
                result.append(text[i:i+2]); i += 2; continue
            if char == '{':
                depth += 1
            elif char == '}':
                if depth == 0:
                    i += 1; continue
                depth -= 1
            result.append(char); i += 1
        return ''.join(result)
    # Fenced examples are deliberately excluded from this compilation.
    chunks = re.split(r'(^ {0,3}(?:`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}(?:`{3,}|~{3,})\s*$)', body, flags=re.MULTILINE)
    return ''.join(chunk if i % 2 else pattern.sub(compile_math, chunk) for i, chunk in enumerate(chunks))


def without_repeated_heading(body, title):
    """The container already prints this title. Preserve code and real subtopics."""
    lines = body.splitlines(keepends=True)
    while lines:
        if not lines[0].strip():
            lines.pop(0)
            continue
        match = re.match(r'^#{1,6}\s+(.+?)\s*#*\s*$', lines[0].strip())
        if match and heading_key(match[1]) == heading_key(title):
            lines.pop(0)
            continue
        break
    return ''.join(lines)


def article_heading_projection(markdown):
    """Keep the immutable article H1, omit only its repeated leading heading."""
    match = re.match(r'^(#\s+([^\n]+)\n)([\s\S]*)$', markdown)
    if not match:
        return markdown
    return match[1] + '\n' + without_repeated_heading(match[3], match[2])
