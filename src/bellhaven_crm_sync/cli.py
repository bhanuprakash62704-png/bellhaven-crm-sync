from __future__ import annotations

import argparse
import json
from pathlib import Path

from .api_client import load_local_env
from .pipeline import refresh_review_queue
from .scraper import scrape_communities
from .webapp import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review Bellhaven website-to-CRM ownership and data changes.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh = subparsers.add_parser(
        "refresh",
        help="Scrape the website and add new CRM discrepancy proposals to the local review queue",
    )
    refresh.add_argument("--db", default="data/review.sqlite3", help="SQLite review database path")

    run_app = subparsers.add_parser("serve", help="Start the local review app")
    run_app.add_argument("--db", default="data/review.sqlite3", help="SQLite review database path")
    run_app.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    run_app.add_argument("--port", type=int, default=8000)

    run_all = subparsers.add_parser("run", help="Refresh proposals, then start the local review app")
    run_all.add_argument("--db", default="data/review.sqlite3", help="SQLite review database path")
    run_all.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    run_all.add_argument("--port", type=int, default=8000)

    scrape = subparsers.add_parser("scrape", help="Scrape all website communities to JSON without CRM access")
    scrape.add_argument("--output", default="data/communities.json", help="Output JSON file path")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "scrape":
        communities = scrape_communities()
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps([community.to_dict() for community in communities], indent=2),
            encoding="utf-8",
        )
        print(f"Scraped {len(communities)} communities to {output}")
        return

    if args.command == "refresh":
        print(json.dumps(refresh_review_queue(args.db), indent=2))
        return

    if args.command == "run":
        print(json.dumps(refresh_review_queue(args.db), indent=2))

    if args.command in {"serve", "run"}:
        load_local_env()
        serve(host=args.host, port=args.port, database_path=args.db)


if __name__ == "__main__":
    main()
