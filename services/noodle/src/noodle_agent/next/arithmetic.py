"""Conservative executable checks for explicit literal arithmetic claims.

Symbolic derivations are outside this check. It never executes generated code.
"""

import ast
import math
import operator
import re

from .planning import QualityFailure

MATH = re.compile(r"(?<!\\)\$\$([\s\S]*?)(?<!\\)\$\$|(?<![\\$])\$(?![\s$])([^$\n]+?)(?<!\s)\$")
OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}


def numeric(value):
    value = value.replace(r"\times", "*").replace(r"\cdot", "*").replace(r"\div", "/")
    value = value.replace("^", "**").replace("{", "(").replace("}", ")").strip()
    if len(value) > 1000 or not re.fullmatch(r"[0-9eE.+*/()\s-]+", value):
        return None
    try:
        tree = ast.parse(value, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 400:
            return None

        def visit(node):
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                return node.value
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
                return (-1 if isinstance(node.op, ast.USub) else 1) * visit(node.operand)
            if isinstance(node, ast.BinOp) and type(node.op) in OPS:
                left, right = visit(node.left), visit(node.right)
                if isinstance(node.op, ast.Pow) and abs(right) > 100:
                    raise ValueError("Exponent budget exceeded")
                result = OPS[type(node.op)](left, right)
                if not math.isfinite(result) or abs(result) > 1e100:
                    raise ValueError("Numeric budget exceeded")
                return result
            raise ValueError("Not a literal arithmetic expression")

        return float(visit(tree.body))
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None


def validate_arithmetic(markdown):
    checked = 0
    for match in MATH.finditer(markdown):
        expression = (match.group(1) or match.group(2)).strip()
        left, right = None, None
        if expression.count("=") == 1:
            left, right = expression.split("=", 1)
        elif re.search(r"[+*/]|\\(?:times|cdot|div)", expression):
            following = markdown[match.end() : match.end() + 80]
            stated = re.match(
                r"\s*[，,。;；]?\s*(?:求和结果为|计算结果为|结果为|等于)\s*\$?(-?\d+(?:\.\d+)?)",
                following,
            )
            if stated:
                left, right = expression, stated.group(1)
        if left is None:
            continue
        actual, claimed = numeric(left), numeric(right)
        if actual is None or claimed is None:
            continue
        checked += 1
        if not math.isclose(actual, claimed, rel_tol=1e-9, abs_tol=1e-9):
            raise QualityFailure(
                f"Literal arithmetic mismatch: {left.strip()} = {actual:g}, but the draft claims {claimed:g}"
            )
    return checked
