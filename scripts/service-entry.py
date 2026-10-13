"""Absolute-path process identity for the cross-platform local supervisor."""

import argparse

from noodle_agent.cli import main

parser = argparse.ArgumentParser()
parser.add_argument("role", choices=["api", "worker"])
parser.add_argument("--port", type=int, default=8000)
args = parser.parse_args()
main(
    ["serve-internal", "--host", "127.0.0.1", "--port", str(args.port)]
    if args.role == "api"
    else ["worker"]
)
