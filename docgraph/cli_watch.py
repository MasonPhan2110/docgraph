from __future__ import annotations

import argparse
import json
import sys

import httpx


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=10.0)


def _print_status(body: dict) -> None:
    print(f"enabled:        {body['enabled']}")
    print(f"running:        {body['running']}")
    print(f"dirs:           {body['dirs_count']}")
    print(f"queue:          {body['queue_depth']}/{body['queue_capacity']}")
    print(f"workers:        {body['workers']}")
    print(f"last_enabled:   {body['last_enabled_at']}")
    s = body["stats"]
    print(f"events recv:    {s['events_received']}")
    print(f"events debc'd:  {s['events_debounced']}")
    print(f"events proc:    {s['events_processed']}")
    print(f"dropped (full): {s['events_dropped_queue_full']}")
    print(f"reconcile runs: {s['reconcile_runs']} (last: {s['last_reconcile_at']})")


def _print_dirs(body: dict) -> None:
    dirs = body["dirs"]
    if not dirs:
        print("(no watched dirs)")
        return
    for d in dirs:
        print(f"{d['id']}  {d['path']}  docs={d['doc_count']}  folder={d['folder']}  tags={d['tags']}")


def run_watch_command(argv: list[str], base_url: str) -> int:
    parser = argparse.ArgumentParser(prog="docgraph watch")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("status")
    sub.add_parser("list")
    sub.add_parser("reconcile")

    p_add = sub.add_parser("add")
    p_add.add_argument("path")
    p_add.add_argument("--folder", default="")
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--ignore", default="")

    p_rm = sub.add_parser("remove")
    p_rm.add_argument("wd_id")
    p_rm.add_argument("--delete-docs", action="store_true")

    args = parser.parse_args(argv)

    try:
        with _client(base_url) as client:
            if args.cmd == "enable":
                r = client.post("/api/watch/enable")
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
            elif args.cmd == "disable":
                r = client.post("/api/watch/disable")
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
            elif args.cmd == "status":
                r = client.get("/api/watch/status")
                r.raise_for_status()
                _print_status(r.json())
            elif args.cmd == "list":
                r = client.get("/api/watch/dirs")
                r.raise_for_status()
                _print_dirs(r.json())
            elif args.cmd == "reconcile":
                r = client.post("/api/watch/reconcile")
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
            elif args.cmd == "add":
                ignore_globs = [g.strip() for g in args.ignore.split(",") if g.strip()]
                r = client.post("/api/watch/dirs", json={
                    "path": args.path,
                    "folder": args.folder,
                    "tags": args.tags,
                    "ignore_globs": ignore_globs,
                })
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
            elif args.cmd == "remove":
                r = client.delete(
                    f"/api/watch/dirs/{args.wd_id}",
                    params={"delete_docs": str(args.delete_docs).lower()},
                )
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
    except httpx.ConnectError:
        print(f"server not running at {base_url}; start with: docgraph serve", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        return 2
    return 0
