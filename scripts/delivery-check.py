"""No-key integration check using isolated real API/Worker/Harness processes.

Fixture parser/LLM output is deliberately synthetic. This does not evaluate
external providers or live LLM tool selection. Artifacts are kept for review.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    run = Path(tempfile.mkdtemp(prefix="noodle-delivery-"))
    # Do not load the developer's .env, editable import path, credentials or DB.
    env = {key: value for key, value in os.environ.items() if not (
        key.upper().startswith(("NOODLE_", "DSH_", "LLM_", "MINERU_", "DEEPSEEK_",
                                "DASHSCOPE_", "EMBEDDING_", "VISION_", "DOUBAO_", "ARK_"))
        or any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET"))
        or key.upper() in {"PYTHONPATH", "PYTHONHOME"}
    )}
    api_port, harness_port = port(), port()
    while harness_port == api_port:
        harness_port = port()
    base = f"http://127.0.0.1:{api_port}"
    env.update({
        "NOODLE_DATA_DIR": str(run / "data"), "NOODLE_PARSER": "fixture",
        "NOODLE_LLM": "fixture", "NOODLE_SKILLS_DIR": str(ROOT / "services/noodle/skills"),
        "NOODLE_HARNESS_WEB_URL": f"http://127.0.0.1:{harness_port}",
        "NOODLE_API_BASE_URL": base, "NOODLE_API_PREFIX": "/internal/v1",
        "NOODLE_RUNTIME_DIR": str(ROOT / ".runtime" / run.name),
        "DSH_HOME": str(ROOT / ".runtime" / run.name / "dsh"),
        "NOODLE_LAUNCH_CWD": str(run),
        "DSH_WEB_HOST": "127.0.0.1", "DSH_WEB_PORT": str(harness_port),
        "NOODLE_HARNESS_WORKSPACE": str(run), "WORKER_POLL_SECONDS": "0.2",
        "PYTHONDONTWRITEBYTECODE": "1", "DELIVERY_OUTPUT_DIR": str(run),
    })
    processes, streams = [], []
    report = {"ok": False, "mode": "offline-fixture", "artifacts": str(run), "checks": []}
    started = time.perf_counter()

    def command(argv, *, cwd=run):
        subprocess.run(argv, cwd=cwd, env=env, check=True, timeout=120)

    def start(name, argv, *, cwd=run):
        stream = (run / f"{name}.log").open("w", encoding="utf-8")
        streams.append(stream)
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream, stderr=stream,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append(process)

    def wait_for(check, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError(f"Service exited; inspect {run}")
            try:
                value = check()
                if value:
                    return value
            except (httpx.HTTPError, KeyError):
                pass
            time.sleep(0.25)
        raise TimeoutError(f"Readiness/result timeout; inspect {run}")

    try:
        command([sys.executable, "-m", "noodle_agent.cli", "create-demo", "--dir", str(run / "input")])
        start("api", [sys.executable, "-m", "noodle_agent.cli", "serve-internal",
                      "--host", "127.0.0.1", "--port", str(api_port)])
        with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
            def get(path):
                response = client.get(path)
                response.raise_for_status()
                return response.json()

            health = wait_for(lambda: get("/health"))
            assert health["legacy_agent_loaded"] is False
            for path in ("/app/", "/app/vendor/pdfjs/pdf.min.mjs",
                         "/app/vendor/pdfjs/pdf.worker.min.mjs"):
                assert client.get(path).status_code == 200, path
            report["checks"].append("API startup and packaged Web/PDF.js assets")
            pdf = run / "input/demo-course.pdf"
            with pdf.open("rb") as stream:
                response = client.post("/internal/v1/sources/pdf",
                                       files={"file": (pdf.name, stream, "application/pdf")})
            assert response.status_code == 202, response.text
            job = response.json()
            # Upload only accepts PDFs. Install the synthetic MinerU sidecar in
            # this isolated test blob store BEFORE Worker starts, not in production.
            attach = """
import shutil
from pathlib import Path
from noodle_agent.config import Settings
from noodle_agent.db import Database, SourceVersion, BlobObject
from noodle_agent.adapters.blob import LocalBlobStore
settings = Settings.from_env()
db = Database(settings.database_url)
with db.session() as session:
    version = session.get(SourceVersion, VERSION)
    blob = session.get(BlobObject, version.original_blob_id)
    target = LocalBlobStore(settings.blob_dir).path_for(blob.storage_key)
    shutil.copyfile(Path('input/demo-course.mineru.json'), target.with_suffix('.mineru.json'))
    shutil.copyfile(Path('input/learning-rate-chart.png'), target.parent / 'learning-rate-chart.png')
db.engine.dispose()
""".replace("VERSION", repr(job["source_version_id"]))
            command([sys.executable, "-c", attach])
            start("worker", [sys.executable, "-m", "noodle_agent.cli", "worker"])
            terminal = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"}

            def finished():
                value = get(f"/internal/v1/jobs/{job['job_id']}")
                return value if value["status"] in terminal else None

            result = wait_for(finished, 90)
            assert result["status"] in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}, result
            timeline = get(f"/internal/v1/jobs/{job['job_id']}/timeline")
            assert timeline
            notes = get("/internal/v1/source-notes")
            assert len(notes) == 1
            note = get(f"/internal/v1/source-notes/{notes[0]['source_note_id']}")
            assert note["unit_count"] > 0 and note["outline"]
            original = client.get(f"/internal/v1/sources/{job['source_id']}/original-content")
            assert original.content.startswith(b"%PDF")
            answer = client.post("/internal/v1/ask", json={"question": "学习率", "limit": 5})
            answer.raise_for_status()
            assert answer.json()["hits"] and answer.json()["hits"][0]["evidence"]
            report["checks"].extend(["HTTP upload -> independent Worker -> SourceNote",
                                     "timeline, original PDF, retrieval and fixture cited answer"])
            report["job_id"] = job["job_id"]
            report["status"] = result["status"]
            report["unit_count"] = note["unit_count"]
            command([shutil.which("node"), str(ROOT / "node_modules/tsx/dist/cli.mjs"),
                     str(ROOT / "scripts/real-api-smoke.ts")], cwd=ROOT)
            report["checks"].append("real Harness tool runtime -> HTTP provider -> isolated API")
            start("harness", [shutil.which("node"), str(ROOT / "scripts/start-harness.mjs")], cwd=ROOT)
            wait_for(lambda: client.get("/internal/v1/harness/sessions").status_code == 200)
            report["checks"].append("fresh Harness Web and same-origin sessions proxy (no LLM turn)")
            if args.browser:
                command([shutil.which("node"), str(ROOT / "scripts/delivery-web.mjs")], cwd=ROOT)
                report["checks"].append("browser reader, PDF.js and Markdown/KaTeX")
            report["ok"] = True
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                # Windows needs a tree stop for the Harness supervisor's child.
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for stream in streams:
            stream.close()
        report["duration_seconds"] = round(time.perf_counter() - started, 2)
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
