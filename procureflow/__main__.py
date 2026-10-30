"""python -m procureflow [serve|jobs] [--db PATH] [--config PATH] [--host H] [--port N] [--no-seed]"""
import argparse
import json
import os

from .db import Database
from .engine import Engine
from .integrations import import_clauses, install, read_clause_file
from .seed import seed
from .server import serve


def main(argv=None):
    parser = argparse.ArgumentParser(prog="procureflow", description="Procurement orchestration tool")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "jobs", "import-clauses"],
                        help="serve: run the web UI/API; jobs: run renewal/expiry jobs once (for cron); "
                             "import-clauses FILE: load SACC clauses from CSV or JSON")
    parser.add_argument("file", nargs="?", help="clause file for import-clauses")
    parser.add_argument("--db", default="procureflow.db", help="SQLite file (default: procureflow.db)")
    parser.add_argument("--config", default=None, help="Workflow JSON (default: built-in workflow.json)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--no-seed", action="store_true", help="Do not load demo data into an empty DB")
    args = parser.parse_args(argv)

    engine = Engine(Database(args.db), args.config)
    if not args.no_seed and seed(engine):
        print("Loaded demo data into", args.db)
    install(engine, f"http://{args.host}:{args.port}")
    if args.command == "import-clauses":
        if not args.file:
            parser.error("import-clauses needs a CSV or JSON file")
        print("Imported", import_clauses(engine, read_clause_file(args.file)), "clauses")
    elif args.command == "jobs":
        print(json.dumps(engine.run_scheduled_jobs(), indent=2))
    else:
        serve(engine, args.host, args.port)


if __name__ == "__main__":
    main()
