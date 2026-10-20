"""Run the bundled front-end KaTeX engine before accepting generated Markdown."""

import json
import shutil
import subprocess
from importlib.resources import files

from .arithmetic import validate_arithmetic
from .planning import QualityFailure

# Static program and JSON stdin: generated content is never interpolated into JS.
PROGRAM = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const context = {module:{exports:{}},exports:{}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const katex=context.module.exports;
let source=JSON.parse(fs.readFileSync(0,'utf8'));
source=source.replace(/```[\s\S]*?```|~~~[\s\S]*?~~~|`+[^\n]*?`+/g,'');
const pattern=/(?<!\\)\$\$([\s\S]*?)(?<!\\)\$\$|\\\[([\s\S]*?)\\\]|\\\(([\s\S]*?)\\\)|(?<![\\$])\$(?![\s$])((?:\\.|[^$\n])+?)(?<!\s)\$/g;
const errors=[];let count=0;
for(const match of source.matchAll(pattern)) {
 const expression=match[1]??match[2]??match[3]??match[4];
 try {katex.renderToString(expression.trim(),{displayMode:match[1]!==undefined||match[2]!==undefined,
  throwOnError:true,strict:'ignore',trust:false,output:'htmlAndMathml'});}
 catch(error) {errors.push({expression,error:error.message});}
 count++;
}
process.stdout.write(JSON.stringify({count,errors}));
"""


def validate_math(markdown: str) -> dict:
    if "$" not in markdown and r"\(" not in markdown and r"\[" not in markdown:
        return {"count": 0, "errors": []}
    node = shutil.which("node")
    if node is None:
        raise QualityFailure("Node.js is required for the KaTeX publication check")
    renderer = files("noodle_agent").joinpath("web", "vendor", "katex.min.js")
    try:
        result = subprocess.run(
            [node, "-e", PROGRAM, str(renderer)],
            input=json.dumps(markdown),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QualityFailure("KaTeX validation could not complete") from exc
    if result.returncode:
        raise QualityFailure("Bundled KaTeX validator failed: " + result.stderr[:300])
    report = json.loads(result.stdout)
    if report["errors"]:
        raise QualityFailure(
            "KaTeX rendering errors: " + json.dumps(report["errors"], ensure_ascii=False)
        )
    report["arithmetic_checks"] = validate_arithmetic(markdown)
    return report
