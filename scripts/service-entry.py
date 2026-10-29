"""Absolute-path process identity for the cross-platform local supervisor."""

import argparse
import os

from noodle_agent.cli import main

parser = argparse.ArgumentParser()
parser.add_argument("role", choices=["api", "worker"])
parser.add_argument("--port", type=int, default=8000)
args = parser.parse_args()
if os.getenv("NOODLE_APP_MODE", "next") != "next":
    raise ValueError("Legacy execution requires its frozen historical workspace")
root = os.environ["NOODLE_DATA_DIR"]
main(["next-api", "--data-dir", root, "--host", "127.0.0.1", "--port", str(args.port)]
     if args.role == "api" else ["next-run", "--data-dir", root])
