from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _first_env(*names: str) -> str | None:
    """Return the first non-empty environment value.

    Provider-specific aliases keep local configuration readable while the
    rest of the application continues to use one OpenAI-compatible adapter.
    The mixed-case DeepSeek names are accepted for compatibility with
    existing .env files; new configuration should prefer uppercase names.
    """

    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    export_dir: Path
    skills_dir: Path
    database_url: str
    parser: str
    llm: str
    mineru_mode: str
    mineru_api_token: str | None
    mineru_model_version: str
    mineru_language: str
    mineru_poll_seconds: float
    llm_api_key: str | None
    llm_base_url: str | None
    llm_model: str | None
    llm_fast_model: str | None
    llm_pro_model: str | None
    llm_timeout_seconds: float
    llm_max_retries: int
    note_language: str
    embedding_api_key: str | None
    embedding_base_url: str | None
    embedding_model: str | None
    embedding_dimensions: int
    qa_min_relevance_score: float
    vision_api_key: str | None
    vision_base_url: str
    vision_model: str
    vision_timeout_seconds: float
    vision_max_retries: int
    vision_reasoning_effort: str
    vision_concurrency: int
    vision_batch_size: int
    vision_max_output_tokens: int
    knowledge_concurrency: int
    planning_single_call_concept_limit: int
    planning_shard_size: int
    planning_concurrency: int
    draft_concurrency: int
    judge_batch_size: int
    judge_concurrency: int
    generation_task_retries: int
    upload_max_bytes: int
    worker_poll_seconds: float
    worker_lease_seconds: int
    worker_heartbeat_seconds: int
    parse_cache_enabled: bool = True
    knowledge_auto_sync: bool = True
    harness_web_url: str = "http://127.0.0.1:3080"

    @classmethod
    def from_env(cls, cwd: Path | None = None) -> Settings:
        base = (cwd or Path.cwd()).resolve()
        _load_dotenv(base / ".env")
        raw_data = Path(os.getenv("NOODLE_DATA_DIR", "./.noodle"))
        data_dir = raw_data if raw_data.is_absolute() else (base / raw_data)
        data_dir = data_dir.resolve()
        raw_export = Path(os.getenv("NOODLE_EXPORT_DIR", str(data_dir / "exports")))
        export_dir = raw_export if raw_export.is_absolute() else (base / raw_export)
        export_dir = export_dir.resolve()
        raw_skills = Path(os.getenv("NOODLE_SKILLS_DIR", "./skills"))
        skills_dir = raw_skills if raw_skills.is_absolute() else (base / raw_skills)
        skills_dir = skills_dir.resolve()
        db_url = os.getenv("NOODLE_DATABASE_URL", f"sqlite:///{data_dir / 'noodle.db'}")
        llm = os.getenv("NOODLE_LLM", "fixture").lower()
        llm_api_key = _first_env("LLM_API_KEY", "DEEPSEEK_API_KEY", "DeepSeek_API_KEY")
        llm_base_url = _first_env("LLM_BASE_URL", "DEEPSEEK_BASE_URL", "DeepSeek_BASE_URL")
        llm_model = _first_env("LLM_MODEL", "DEEPSEEK_MODEL", "DeepSeek_MODEL")
        fast_model = _first_env("LLM_FAST_MODEL", "DEEPSEEK_FAST_MODEL") or llm_model
        pro_model = _first_env("LLM_PRO_MODEL", "DEEPSEEK_PRO_MODEL") or llm_model or fast_model
        if llm == "deepseek":
            fast_model = fast_model or "deepseek-v4-flash"
            pro_model = pro_model or "deepseek-v4-pro"
            llm_model = llm_model or fast_model

        embedding_api_key = _first_env("EMBEDDING_API_KEY", "DASHSCOPE_API_KEY")
        embedding_base_url = _first_env("EMBEDDING_BASE_URL", "DASHSCOPE_BASE_URL")
        embedding_model = _first_env("EMBEDDING_MODEL")
        if _first_env("DASHSCOPE_API_KEY"):
            embedding_base_url = (
                embedding_base_url
                or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            )
            embedding_model = embedding_model or "text-embedding-v4"

        return cls(
            data_dir=data_dir,
            export_dir=export_dir,
            skills_dir=skills_dir,
            database_url=db_url,
            parser=os.getenv("NOODLE_PARSER", "fixture").lower(),
            llm=llm,
            mineru_mode=os.getenv("MINERU_MODE", "lightweight").lower(),
            mineru_api_token=os.getenv("MINERU_API_TOKEN") or None,
            mineru_model_version=os.getenv("MINERU_MODEL_VERSION", "vlm"),
            mineru_language=os.getenv("MINERU_LANGUAGE", "ch"),
            mineru_poll_seconds=float(os.getenv("MINERU_POLL_SECONDS", "5")),
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_fast_model=fast_model,
            llm_pro_model=pro_model,
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
            llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "0")),
            note_language=os.getenv("NOTE_LANGUAGE", "zh-CN"),
            embedding_api_key=embedding_api_key,
            embedding_base_url=embedding_base_url,
            embedding_model=embedding_model,
            embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "1024")),
            qa_min_relevance_score=min(
                1.0, max(0.0, float(os.getenv("QA_MIN_RELEVANCE_SCORE", "0.22")))
            ),
            vision_api_key=_first_env("VISION_API_KEY", "DOUBAO_API_KEY", "ARK_API_KEY"),
            vision_base_url=os.getenv(
                "VISION_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
            ),
            vision_model=os.getenv(
                "VISION_MODEL", "doubao-seed-2-1-turbo-260628"
            ),
            vision_timeout_seconds=float(os.getenv("VISION_TIMEOUT_SECONDS", "120")),
            vision_max_retries=max(0, int(os.getenv("VISION_MAX_RETRIES", "2"))),
            vision_reasoning_effort=os.getenv("VISION_REASONING_EFFORT", "low"),
            vision_concurrency=max(1, int(os.getenv("VISION_CONCURRENCY", "4"))),
            vision_batch_size=max(1, int(os.getenv("VISION_BATCH_SIZE", "4"))),
            vision_max_output_tokens=max(
                256, int(os.getenv("VISION_MAX_OUTPUT_TOKENS", "900"))
            ),
            knowledge_concurrency=max(
                1, int(os.getenv("KNOWLEDGE_CONCURRENCY", "4"))
            ),
            planning_single_call_concept_limit=max(
                1, int(os.getenv("PLANNING_SINGLE_CALL_CONCEPT_LIMIT", "18"))
            ),
            planning_shard_size=max(
                2, int(os.getenv("PLANNING_SHARD_SIZE", "8"))
            ),
            planning_concurrency=max(
                1, int(os.getenv("PLANNING_CONCURRENCY", "3"))
            ),
            draft_concurrency=max(1, int(os.getenv("DRAFT_CONCURRENCY", "4"))),
            judge_batch_size=max(1, int(os.getenv("JUDGE_BATCH_SIZE", "4"))),
            judge_concurrency=max(1, int(os.getenv("JUDGE_CONCURRENCY", "4"))),
            generation_task_retries=max(0, int(os.getenv("GENERATION_TASK_RETRIES", "1"))),
            upload_max_bytes=max(
                1_048_576, int(os.getenv("UPLOAD_MAX_BYTES", str(100 * 1024 * 1024)))
            ),
            worker_poll_seconds=max(0.2, float(os.getenv("WORKER_POLL_SECONDS", "1"))),
            worker_lease_seconds=max(30, int(os.getenv("WORKER_LEASE_SECONDS", "180"))),
            worker_heartbeat_seconds=max(
                5, int(os.getenv("WORKER_HEARTBEAT_SECONDS", "30"))
            ),
            parse_cache_enabled=os.getenv("PARSE_CACHE_ENABLED", "true").lower() in {"1", "true", "yes"},
            knowledge_auto_sync=os.getenv("KNOWLEDGE_AUTO_SYNC", "true").lower() in {"1", "true", "yes"},
            harness_web_url=os.getenv(
                "NOODLE_HARNESS_WEB_URL", "http://127.0.0.1:3080"
            ).rstrip("/"),
        )

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
