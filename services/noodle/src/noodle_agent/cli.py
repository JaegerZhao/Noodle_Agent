"""Current next-v1 application entry point; legacy stores are never opened implicitly."""

import argparse
import json
import sys
from pathlib import Path


def emit(value):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="noodle", description="Noodle PDF knowledge workspace"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("next-schema", help="Print current content and retrieval contracts")
    for name in ["next-init", "db-upgrade", "db-status"]:
        command = sub.add_parser(name)
        command.add_argument("--data-dir", type=Path, required=True)
    from .next.cli import configure

    configure(sub)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "next-schema":
        from .next.concepts.contracts import Decision
        from .next.concepts.review import Resolution
        from .next.contracts import Candidate, Review
        from .next.retrieval.contracts import AnswerResponse, Query, SearchResponse

        emit(
            {
                "contract": "next-v1",
                "candidate": Candidate.model_json_schema(),
                "review": Review.model_json_schema(),
                "concept_decision": Decision.model_json_schema(),
                "contribution_resolution": Resolution.model_json_schema(),
                "rag_query": Query.model_json_schema(),
                "rag_search": SearchResponse.model_json_schema(),
                "rag_answer": AnswerResponse.model_json_schema(),
            }
        )
    elif args.command in {"next-init", "db-upgrade", "db-status"}:
        from .storage.database import Database
        from .storage.runner import status

        if args.command == "db-status":
            path = args.data_dir.resolve() / "noodle.db"
            if not path.is_file():
                raise FileNotFoundError("No existing knowledge store: " + str(path))
            database = Database("sqlite:///" + str(path), contract="next-v1")
            try:
                emit(status(database.engine, contract="next-v1"))
            finally:
                database.close()
        else:
            from .next.store import Store

            store = Store(args.data_dir)
            try:
                emit(
                    {
                        "contract": store.contract,
                        "data_dir": str(store.root),
                        **store.db.last_migration,
                    }
                )
            finally:
                store.close()
    else:
        from .next.cli import run

        run(args, emit)


if __name__ == "__main__":
    main()
