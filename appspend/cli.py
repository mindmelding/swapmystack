"""Command line: appspend audit | scan | history | catalog."""

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

    out_html = Path(args.out) if args.out else Path(f"appspend-{_slug(audit.store)}-{date.today()}.html")
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


def _status(msg: str) -> None:
    print(msg, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="appspend", description="Find wasted Shopify app spend.")
    p.add_argument("--version", action="version", version=f"appspend {__version__}")
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

    h = sub.add_parser("history", help="past audits for a store")
    h.add_argument("store")
    h.add_argument("--db")
    h.set_defaults(func=cmd_history)

    c = sub.add_parser("catalog", help="list the apps appspend can recognize")
    c.add_argument("query", nargs="?")
    c.set_defaults(func=cmd_catalog)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"appspend: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
