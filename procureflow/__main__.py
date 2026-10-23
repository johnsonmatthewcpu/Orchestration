"""python -m procureflow [serve|jobs] [--db PATH] [--config PATH] [--host H] [--port N] [--no-seed]"""
import argparse
import json

from .db import Database
from .engine import Engine
from .seed import seed
from .server import serve


def main(argv=None):
    parser = argparse.ArgumentParser(prog="procureflow", description="Procurement orchestration tool")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "jobs"],
                        help="serve: run the web UI/API; jobs: run renewal/expiry jobs once (for cron)")
    parser.add_argument("--db", default="procureflow.db", help="SQLite file (default: procureflow.db)")
    parser.add_argument("--config", default=None, help="Workflow JSON (default: built-in workflow.json)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-seed", action="store_true", help="Do not load demo data into an empty DB")
    args = parser.parse_args(argv)

    engine = Engine(Database(args.db), args.config)
    if not args.no_seed and seed(engine):
        print("Loaded demo data into", args.db)
    if args.command == "jobs":
        print(json.dumps(engine.run_scheduled_jobs(), indent=2))
    else:
        serve(engine, args.host, args.port)


if __name__ == "__main__":
    main()
