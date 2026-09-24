"""Command line: swapstack audit | scan | batch | history | catalog | migrate | mcp."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import __version__, history
from .analyze import analyze
from .bills import parse_bills
from .fingerprints import Catalog
from .report import render_html, render_json, render_markdown, render_text
from .scan import normalize_store, scan_storefront
from .theme import scan_theme


def _slug(store: str | None) -> str:
    return (normalize_store(store).replace("https://", "").replace("www.", "").replace(".", "-")) if store else "store"


def cmd_audit(args: argparse.Namespace) -> int:
    if not (args.store or args.theme or args.bills):
        print("Give at least one of: a store domain, --theme, --bills", file=sys.stderr)
        return 2
    catalog = Catalog.load(args.catalog)

    scan = theme = bills = None
    if args.store:
        _status(f"Reading {normalize_store(args.store)} ...")
        scan = scan_storefront(args.store, catalog, pages=args.pages)
        if not any(p["status"] == 200 for p in scan.pages):
            errs = "; ".join(f"{p['url']}: {p['error'] or p['status']}" for p in scan.pages)
            print(f"Could not read the storefront ({errs}).", file=sys.stderr)
            if not (args.theme or args.bills):
                return 1
            scan = None
        elif not scan.is_shopify:
            print("Warning: this doesn't look like a Shopify storefront. Results may be thin.", file=sys.stderr)
    if args.theme:
        _status(f"Reading theme {args.theme} ...")
        theme = scan_theme(args.theme, catalog)
    if args.bills:
        _status(f"Reading bills {args.bills} ...")
        bills = parse_bills(args.bills, catalog)

    audit = analyze(catalog, scan, theme, bills, pages_checked=len(scan.pages) if scan else 0)
    data = audit.to_dict()

    print(render_text(audit))

    out_html = Path(args.out) if args.out else Path(f"swapstack-{_slug(audit.store)}-{date.today()}.html")
    out_html.write_text(render_html(audit))
    print(f"\nReport: {out_html.resolve()}")
    if args.json:
        Path(args.json).write_text(render_json(audit))
        print(f"JSON:   {Path(args.json).resolve()}")
    if args.md:
        Path(args.md).write_text(render_markdown(audit))
        print(f"Markdown: {Path(args.md).resolve()}")

    if not args.no_history:
        with history.connect(args.db) as conn:
            diff = history.save(conn, data)
        if diff and (diff["added"] or diff["removed"]):
            print(f"\nSince {diff['since'][:10]}: added {', '.join(diff['added']) or 'none'}; removed {', '.join(diff['removed']) or 'none'}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    catalog = Catalog.load(args.catalog)
    scan = scan_storefront(args.store, catalog, pages=args.pages)
    if args.json:
        import json
        print(json.dumps(scan.to_dict(), indent=2))
        return 0
    print(f"{scan.store}  ({sum(1 for p in scan.pages if p['status'] == 200)}/{len(scan.pages)} pages read)")
    for app_id, evs in sorted(scan.detections.items(), key=lambda kv: catalog.apps[kv[0]].category):
        app = catalog.apps[app_id]
        print(f"  {app.name:<28} {catalog.categories[app.category].label:<28} {evs[0].source}: {evs[0].detail}")
    if scan.unknown_handles:
        print(f"  unrecognized handles: {', '.join(scan.unknown_handles)}")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    from .batch import read_domains, run_batch, write_csv

    catalog = Catalog.load(args.catalog)
    rows = read_domains(args.domains)
    if not rows:
        print("No domains found in that file.", file=sys.stderr)
        return 2
    out = Path(args.out or Path(args.domains).with_name(Path(args.domains).stem + "-scan.csv"))

    def progress(i, n, r):
        tag = f"{len(r.paid_apps)} paid apps" + (f", {len(r.overlaps)} overlap" if r.overlaps else "") if r.shopify else r.status
        _status(f"[{i}/{n}] {r.domain}: {tag}")

    ranked = run_batch(rows, catalog, pages=args.pages, delay=args.delay, progress=progress)
    write_csv(ranked, out)
    ok = [r for r in ranked if r.shopify]
    print(f"\n{len(ok)} of {len(ranked)} are Shopify storefronts. {sum(1 for r in ok if r.overlaps)} show an overlap.\n")
    for r in ranked[: args.top]:
        if r.shopify and r.score:
            print(f"  {r.score:>3}  {r.domain:<32} {r.pitch or ', '.join(r.paid_apps[:4])}")
    print(f"\nFull results: {out.resolve()}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    with history.connect(args.db) as conn:
        rows = history.runs(conn, normalize_store(args.store))
    if not rows:
        print("No runs recorded for that store.")
        return 0
    for when, n, spend, save in rows:
        spend_s = f"${spend:,.0f}/mo" if spend is not None else "—"
        print(f"{when}  {n:>3} apps  spend {spend_s:<10}  likely savings ${save or 0:,.0f}/mo")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    catalog = Catalog.load(args.catalog)
    q = (args.query or "").lower()
    by_cat: dict[str, list[str]] = {}
    for app in catalog.apps.values():
        if q and q not in app.name.lower() and q not in app.category:
            continue
        by_cat.setdefault(app.category, []).append(app.name)
    print(f"catalog {catalog.version}: {len(catalog.apps)} apps in {len(catalog.categories)} categories\n")
    for cid, names in sorted(by_cat.items()):
        print(f"{catalog.categories[cid].label:<30} {', '.join(sorted(names))}")
    return 0


MARK = {"done": "x", "skipped": "-", "waiting": "~", "failed": "!", "pending": " "}


def _print_step(d: dict) -> None:
    print(f"\n[{d['kind']}] {d['title']}  ({d['step']})")
    print(f"  {d['instructions']}")
    if d.get("loses"):
        print("  What doesn't carry over:")
        for x in d["loses"]:
            print(f"   - {x}")
    for name, state in (d.get("env") or {}).items():
        print(f"  env {name}: {state}")
    for i in d.get("inputs") or []:
        print(f"  input {i['name']}{' (optional)' if i.get('optional') else ''}: {'have it' if i['have'] else 'needed'}  (--input {i['name']}=PATH)")
    if d.get("message"):
        m = d["message"]
        print(f"  Draft to {m['to']}\n  Subject: {m['subject']}\n")
        print("    " + m["body"].replace("\n", "\n    "))
    if d.get("waits"):
        print(f"  Waits on: {d['waits']}")
    if d.get("rollback"):
        print(f"  Rollback: {d['rollback']}")
    res = d.get("result")
    if res:
        print(f"  Last result ({'ok' if res['ok'] else 'failed'}): {res['summary']}")
    verb = "run" if d.get("runnable") else "done"
    tail = ' --approved-by "NAME"' if d["kind"] == "approve" else ""
    print(f"\n  Next: swapstack migrate {verb} {d['migration']} {d['step']}{tail}"
          + (f"   (or: swapstack migrate skip {d['migration']} {d['step']} --reason ...)" if d["optional"] else ""))


def cmd_migrate(args: argparse.Namespace) -> int:
    import json
    from .migrate import MigrationError, Runner

    r = Runner()
    try:
        if args.op == "playbooks":
            for pb in r.playbooks.values():
                apps = ", ".join(f"{k} ({v.get('name', k)})" for k, v in pb.variants.items())
                print(f"{pb.id:<26} {pb.title}\n{'':<26} from: {apps}")
            return 0
        if args.op == "list":
            for m in r.all():
                cur = r.current(m)
                print(f"{m.id:<48} next: {cur.id if cur else 'finished'}")
            return 0
        if args.op == "start":
            m = r.start(args.target, args.playbook, args.from_app)
        else:
            m = r.load(args.target)
        for kv in args.input or []:
            name, _, value = kv.partition("=")
            r.set_input(m, name.strip(), value.strip())
        if args.op == "run":
            res = r.run(m, args.step)
            print(f"{'ok' if res['ok'] else 'FAILED'}: {res['summary']}")
            for f in res["files"]:
                print(f"  wrote {f}")
        elif args.op == "done":
            r.complete(m, args.step, args.note or "", args.approved_by or "")
        elif args.op == "wait":
            r.wait(m, args.step, args.note or "")
        elif args.op == "skip":
            r.skip(m, args.step, args.reason or "skipped")
        elif args.op == "draft":
            print(f"Draft written: {r.draft(m, args.step)}")
            return 0
        st = r.status(m)
        if args.json:
            print(json.dumps(st, indent=1, default=str))
            return 0
        print(f"{st['migration']}  ({st['playbook']})")
        for s in st["steps"]:
            print(f"  [{MARK.get(s['status'], '?')}] {s['title']}{' (optional)' if s['optional'] else ''}")
        if st["next"]:
            _print_step(st["next"])
        else:
            print("\nFinished.")
        return 0
    except MigrationError as e:
        print(f"swapstack: {e}", file=sys.stderr)
        return 2


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp import serve
    serve()
    return 0


def _status(msg: str) -> None:
    print(msg, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swapstack", description="Find wasted Shopify app spend.")
    p.add_argument("--version", action="version", version=f"swapstack {__version__}")
    p.add_argument("--catalog", help="use a custom fingerprint catalog JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="full audit: storefront + optional theme and bills, writes an HTML report")
    a.add_argument("store", nargs="?", help="store domain, e.g. example.com or example.myshopify.com")
    a.add_argument("--bills", help="Shopify bills export CSV, or a CSV with app,monthly_cost")
    a.add_argument("--theme", help="downloaded theme .zip or folder")
    a.add_argument("--pages", type=int, default=3, help="storefront pages to read (default 3: home, product, collection)")
    a.add_argument("--out", help="HTML report path")
    a.add_argument("--json", help="also write the full audit as JSON")
    a.add_argument("--md", help="also write a Markdown summary")
    a.add_argument("--db", help=f"history database (default {history.DEFAULT_DB})")
    a.add_argument("--no-history", action="store_true", help="don't record this run")
    a.set_defaults(func=cmd_audit)

    s = sub.add_parser("scan", help="quick storefront-only app detection")
    s.add_argument("store")
    s.add_argument("--pages", type=int, default=3)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scan)

    bt = sub.add_parser("batch", help="scan a list of storefronts and rank them by likely findings")
    bt.add_argument("domains", help=".txt with one domain per line, or a CSV with a 'domain' column")
    bt.add_argument("--out", help="results CSV (default: <input>-scan.csv)")
    bt.add_argument("--pages", type=int, default=2, help="pages per store (default 2)")
    bt.add_argument("--delay", type=float, default=1.0, help="seconds between stores (default 1)")
    bt.add_argument("--top", type=int, default=20, help="rows to print (default 20)")
    bt.set_defaults(func=cmd_batch)

    h = sub.add_parser("history", help="past audits for a store")
    h.add_argument("store")
    h.add_argument("--db")
    h.set_defaults(func=cmd_history)

    c = sub.add_parser("catalog", help="list the apps swapstack can recognize")
    c.add_argument("query", nargs="?")
    c.set_defaults(func=cmd_catalog)
    mg = sub.add_parser("migrate", help="walk through a guided migration to a cheaper tool")
    mg.add_argument("op", choices=["playbooks", "list", "start", "status", "run", "done", "wait", "skip", "draft"])
    mg.add_argument("target", nargs="?", help="store domain (start) or migration id")
    mg.add_argument("step", nargs="?", help="step id (run, done, wait, skip, draft)")
    mg.add_argument("--playbook", help="playbook id (start)")
    mg.add_argument("--from", dest="from_app", help="catalog id of the app being left (start), e.g. yotpo")
    mg.add_argument("--input", action="append", help="name=value, e.g. export_file=~/Downloads/reviews.csv")
    mg.add_argument("--note")
    mg.add_argument("--approved-by", help="who said yes (approve steps)")
    mg.add_argument("--reason")
    mg.add_argument("--json", action="store_true", help="print status as JSON")
    mg.set_defaults(func=cmd_migrate)

    mc = sub.add_parser("mcp", help="run the MCP server on stdio for AI assistants")
    mc.set_defaults(func=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"swapstack: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
