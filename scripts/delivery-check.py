"""Offline current-workflow acceptance plus isolated API/worker/Harness startup.

Content providers are injected test doubles; no credentials or production data
are passed to these subprocesses. This does not assess live model quality.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    run=Path(tempfile.mkdtemp(prefix="noodle-current-delivery-"))
    env={k:v for k,v in os.environ.items() if not k.upper().startswith(("NOODLE_","DSH_","LLM_","MINERU_","DEEPSEEK_","DASHSCOPE_","EMBEDDING_","VISION_","DOUBAO_","ARK_")) and not any(x in k.upper() for x in ("API_KEY","TOKEN","SECRET","PYTHONPATH","PYTHONHOME"))}
    env.update({"PYTHONPATH":str(ROOT/"services/noodle/src"),"PYTHONDONTWRITEBYTECODE":"1","NOODLE_PYTHON":sys.executable})
    tests=["test_next_source_sections.py","test_next_convergence.py","test_next_entity_concepts.py","test_next_workflow_readiness.py","test_next_workspace.py"]
    subprocess.run([sys.executable,"-m","pytest",*[str(ROOT/"services/noodle/tests"/n) for n in tests],"-q","-p","no:cacheprovider","--basetemp",str(run/"pytest"),"--junitxml",str(run/"workflow-tests.xml")],cwd=run,env=env,check=True)
    node=shutil.which("node")
    if not node:raise RuntimeError("Node.js is required for launcher acceptance")
    subprocess.run([node,str(ROOT/"scripts/launcher-check.mjs")],cwd=ROOT,env=env,check=True)
    report={"ok":True,"mode":"current-next-only","providers":"INJECTED_OFFLINE","separate_api_worker_harness":True,"artifacts":str(run)}
    (run/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report),flush=True)

if __name__=="__main__":main()
