"""Local code checks; no execution, imported packages, or external services."""

import ast
import json
import re

from .section_contracts import Finding


def code_findings(records):
    findings = []
    for record in records:
        loads, defined = set(), set()
        for section in record["draft"]["sections"]:
            for block in re.findall(
                r"```(?:python|py)\s*\n(.*?)```", section["markdown"], re.DOTALL
            ):
                try:
                    tree = ast.parse(block.strip())
                except SyntaxError as exc:
                    findings.append(
                        Finding(
                            plan_key=record["key"],
                            section_key=section["key"],
                            issue=f"Python示例存在语法错误（第{exc.lineno}行：{exc.msg}），请对照本页完整代码恢复，不执行代码。",
                        )
                    )
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name):
                        (loads if isinstance(node.ctx, ast.Load) else defined).add(node.id)
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        defined.add(node.name)
                    elif isinstance(node, ast.arg):
                        defined.add(node.arg)
                    elif isinstance(node, (ast.Import, ast.ImportFrom)):
                        defined.update(a.asname or a.name.split(".")[0] for a in node.names)
        # An explanatory note may show initialization inline and a later
        # operation as a fenced example. Both count as supplied code; prose is
        # never executed and malformed inline fragments are not inferred.
        for section in record["draft"]["sections"]:
            for snippet in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", section["markdown"]):
                if not re.match(r"^[A-Za-z_]\w*\s*=(?!=)", snippet):
                    continue
                try:
                    inline = ast.parse(snippet)
                except SyntaxError:
                    continue
                defined.update(
                    node.id
                    for node in ast.walk(inline)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                )
        source_defs = set()
        definition_lines = {}
        for context in record.get("contexts", []):
            raw = json.loads(context["text"])
            ocr = "\n".join(raw.get("ocr_text") or [])
            for line in ocr.splitlines():
                assignment = re.match(r"^\s*([A-Za-z_]\w*)\s*=", line)
                if assignment:
                    source_defs.add(assignment[1])
                    definition_lines[assignment[1]] = line.strip()
            source_defs.update(re.findall(r"^\s*def\s+([A-Za-z_]\w*)\s*\(", ocr, re.MULTILINE))
        missing = sorted((loads - defined) & source_defs)
        if missing:
            findings.append(
                Finding(
                    plan_key=record["key"],
                    issue="生成代码使用了本节原图中已给出、但笔记遗漏初始化或定义的名称："
                    + ", ".join(missing)
                    + "。仅描述构造器名称不等于给变量赋值。必须在实际Python代码围栏或完整行内赋值中补回原页已有初始化，不编造外部初始化。可核对的原页行："
                    + json.dumps(
                        {name: definition_lines.get(name, "原页函数定义") for name in missing},
                        ensure_ascii=False,
                    ),
                )
            )
    return findings
