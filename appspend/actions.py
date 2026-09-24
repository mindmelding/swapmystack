"""The steps appspend runs itself during a migration.

Each action takes a context (the migration, a work folder, env access, an HTTP fetcher) and the step's
inputs, and returns {"ok", "summary", "data", "files"}. Actions read exports and call APIs with keys from
environment variables. They never print or store a key, and they never write to a vendor: every write
that changes a live store is a step the merchant or the vendor performs.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import math
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__

HttpFetcher = Callable[[str, dict], tuple[int, str]]
USER_AGENT = f"appspend/{__version__} (migration assistant)"


def http_get(url: str, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace") if e.fp else ""
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, str(e)


@dataclass
class Context:
    store: str
    variant: str
    workdir: Path
    results: dict = field(default_factory=dict)       # earlier steps' data, by step id
    env: Callable[[str], str | None] = os.environ.get
    fetch: HttpFetcher = http_get
    params: dict = field(default_factory=dict)


def _result(ok: bool, summary: str, data: dict | None = None, files: list[str] | None = None) -> dict:
    return {"ok": ok, "summary": summary, "data": data or {}, "files": files or []}


def _missing_env(ctx: Context, names: list[str]) -> dict | None:
    missing = [n for n in names if not ctx.env(n)]
    if missing:
        return _result(False, "Set these environment variables in your shell first (don't paste keys into chat): "
                       + ", ".join(missing))
    return None


# --- CSV helpers --------------------------------------------------------------------------------

def read_csv(path: str | Path) -> tuple[list[str], list[dict]]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no file at {p}")
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = [r for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))]
    return [h for h in (reader.fieldnames or []) if h], rows


def _key(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


def pick(headers: list[str], *hints: str, avoid: tuple[str, ...] = ()) -> str | None:
    """The first header matching a hint: exact (normalized) matches win over substring matches."""
    norm = {h: _key(h) for h in headers}
    for hint in hints:
        k = _key(hint)
        for h, n in norm.items():
            if n == k:
                return h
    for hint in hints:
        k = _key(hint)
        for h, n in norm.items():
            if k in n and not any(_key(a) in n for a in avoid):
                return h
    return None


def _urls(value: str) -> list[str]:
    return re.findall(r"https?://[^\s,;|\"']+", value or "")


def _n(count: int, one: str, many: str) -> str:
    """'1 review has' / '3 reviews have': pass the noun phrase with its verb for both forms."""
    return f"{count:,} {one if count == 1 else many}"


def _num(value: str) -> float | None:
    v = re.sub(r"[^0-9.\-]", "", value or "")
    try:
        return float(v) if v not in ("", "-", ".") else None
    except ValueError:
        return None


# --- Reviews → Judge.me -------------------------------------------------------------------------

JUDGEME_BATCH = 5000  # Judge.me recommends about 5,000 reviews per import file


def reviews_inspect_export(ctx: Context, inputs: dict) -> dict:
    """What will and won't carry over, before anything is imported."""
    headers, rows = read_csv(inputs["export_file"])
    body = pick(headers, "body", "review_content", "content", "review body", "review text", "review", "comment",
                avoid=("title", "reply", "date", "id"))
    rating = pick(headers, "rating", "review_score", "score", "stars", "review rating", avoid=("count", "average"))
    product = pick(headers, "product_id", "product id", "productid", "product_handle", "product handle", "sku")
    meta = pick(headers, "metaobject_handle", "metaobjecthandle", "metaobject")
    photo_cols = [h for h in headers if re.search(r"image|picture|photo", h, re.I)]
    video_cols = [h for h in headers if re.search(r"video", h, re.I)]
    if not rating:
        return _result(False, f"Couldn't find a rating column in {', '.join(headers[:12])}. Is this the reviews export?")

    counts = dict(total=len(rows), importable=0, rating_only=0, bad_rating=0, with_video=0, photos_dropped=0,
                  store_reviews=0, no_metaobject=0)
    for r in rows:
        score = _num(r.get(rating, ""))
        text = (r.get(body, "") if body else "").strip()
        if score is None or not 1 <= score <= 5:
            counts["bad_rating"] += 1
            continue
        if not text:
            counts["rating_only"] += 1
            continue
        counts["importable"] += 1
        if any((r.get(c) or "").strip() for c in video_cols):
            counts["with_video"] += 1
        photos = sum(len(_urls(r.get(c, ""))) or (1 if (r.get(c) or "").strip() else 0) for c in photo_cols)
        counts["photos_dropped"] += max(0, photos - 5)
        pid = (r.get(product, "") if product else "").strip().lower()
        if not pid or pid == "yotpo_site_reviews":
            counts["store_reviews"] += 1
        if meta is not None and not (r.get(meta) or "").strip():
            counts["no_metaobject"] += 1

    files: list[str] = []
    if len(rows) > JUDGEME_BATCH:
        src = Path(inputs["export_file"]).expanduser()
        for i in range(math.ceil(len(rows) / JUDGEME_BATCH)):
            out = ctx.workdir / f"{src.stem}-part{i + 1}.csv"
            with open(out, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=headers)
                w.writeheader()
                w.writerows(rows[i * JUDGEME_BATCH:(i + 1) * JUDGEME_BATCH])
            files.append(str(out))

    lines = [f"{counts['importable']:,} of {counts['total']:,} reviews will import."]
    if counts["rating_only"]:
        lines.append(f"{_n(counts['rating_only'], 'has', 'have')} a rating but no text; Judge.me rejects those.")
    if counts["bad_rating"]:
        lines.append(f"{_n(counts['bad_rating'], 'has', 'have')} no usable 1 to 5 rating.")
    if counts["with_video"]:
        lines.append(f"{_n(counts['with_video'], 'has a video', 'have videos')}, which don't import; re-upload them by hand after.")
    if counts["photos_dropped"]:
        lines.append(f"{_n(counts['photos_dropped'], 'photo', 'photos')} beyond the 5-per-review limit will be dropped.")
    if counts["store_reviews"]:
        lines.append(f"{_n(counts['store_reviews'], 'is a store review', 'are store reviews')}, not product reviews.")
    if meta is None and ctx.variant in ("yotpo", "stamped"):
        lines.append("The file has no metaobject handle column, so Judge.me can't mark these reviews verified.")
    elif counts["no_metaobject"]:
        lines.append(f"{_n(counts['no_metaobject'], 'has', 'have')} no metaobject handle and will import unverified.")
    if files:
        lines.append(f"Split into {len(files)} files of up to {JUDGEME_BATCH:,} reviews. Import each file once.")
    columns = {"body": body, "rating": rating, "product": product, "metaobject": meta,
               "photos": photo_cols, "videos": video_cols}
    return _result(True, " ".join(lines), {**counts, "columns": columns, "batches": len(files) or 1}, files)


def reviews_count_judgeme(ctx: Context, inputs: dict) -> dict:
    """Count reviews through Judge.me's API. mode=baseline before import, mode=verify after."""
    if err := _missing_env(ctx, ["JUDGEME_API_TOKEN", "JUDGEME_SHOP_DOMAIN"]):
        return err
    q = urllib.parse.urlencode({"api_token": ctx.env("JUDGEME_API_TOKEN"), "shop_domain": ctx.env("JUDGEME_SHOP_DOMAIN")})
    status, body = ctx.fetch(f"https://api.judge.me/api/v1/reviews/count?{q}", {})
    if status != 200:
        return _result(False, f"Judge.me answered {status}. Check the private token and the myshopify.com domain.")
    try:
        payload = json.loads(body)
        count = int(payload["count"] if isinstance(payload, dict) and "count" in payload else payload)
    except (ValueError, KeyError, TypeError):
        return _result(False, f"Unexpected answer from Judge.me: {body[:120]}")

    if ctx.params.get("mode") != "verify":
        return _result(True, f"Judge.me has {count:,} reviews before the import.", {"count": count})
    baseline = (ctx.results.get("baseline") or {}).get("count", 0)
    expected = (ctx.results.get("inspect") or {}).get("importable")
    added = count - baseline
    if expected is None:
        return _result(added > 0, f"{added:,} reviews arrived (no inspect result to compare against).", {"count": count, "added": added})
    ok = added >= math.floor(expected * 0.95)
    summary = f"{added:,} of the {expected:,} importable reviews arrived."
    if not ok:
        summary += (" That's short. Check the Reviews Import Log in Judge.me for rejected rows, and wait for any"
                    " remaining files to finish. Don't re-import a file that already went in: it creates duplicates.")
    elif added > expected:
        summary += " More than expected: check the import log for a file imported twice."
    return _result(ok, summary, {"count": count, "added": added, "expected": expected})


# --- Helpdesk → Commslayer ----------------------------------------------------------------------

def _basic(user: str, secret: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()}


def _paged_gorgias(ctx: Context, base: str, path: str, auth: dict, cap: int = 2000) -> list[dict]:
    items: list[dict] = []
    cursor = None
    while len(items) < cap:
        url = f"{base}{path}?limit=100" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        status, body = ctx.fetch(url, auth)
        if status != 200:
            raise RuntimeError(f"Gorgias {path} answered {status}")
        payload = json.loads(body)
        if isinstance(payload, list):
            return items + payload
        items += payload.get("data") or []
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return items


def _paged_zendesk(ctx: Context, url: str, key: str, auth: dict, cap: int = 2000) -> list[dict]:
    items: list[dict] = []
    while url and len(items) < cap:
        status, body = ctx.fetch(url, auth)
        if status != 200:
            raise RuntimeError(f"Zendesk answered {status} for {url.split('?')[0]}")
        payload = json.loads(body)
        items += payload.get(key) or []
        url = payload.get("next_page")
    return items


def helpdesk_inventory(ctx: Context, inputs: dict) -> dict:
    """List the rules and saved replies that won't migrate, as a rebuild sheet for the new helpdesk."""
    sections: list[tuple[str, list[dict]]] = []
    try:
        if ctx.variant == "gorgias":
            if err := _missing_env(ctx, ["GORGIAS_DOMAIN", "GORGIAS_EMAIL", "GORGIAS_API_KEY"]):
                return err
            base = f"https://{ctx.env('GORGIAS_DOMAIN').removesuffix('.gorgias.com')}.gorgias.com"
            auth = _basic(ctx.env("GORGIAS_EMAIL"), ctx.env("GORGIAS_API_KEY"))
            sections.append(("Rules", _paged_gorgias(ctx, base, "/api/rules", auth)))
            sections.append(("Macros (saved replies)", _paged_gorgias(ctx, base, "/api/macros", auth)))
        elif ctx.variant == "zendesk":
            if err := _missing_env(ctx, ["ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN"]):
                return err
            base = f"https://{ctx.env('ZENDESK_SUBDOMAIN').removesuffix('.zendesk.com')}.zendesk.com/api/v2"
            auth = _basic(f"{ctx.env('ZENDESK_EMAIL')}/token", ctx.env("ZENDESK_API_TOKEN"))
            sections.append(("Triggers", _paged_zendesk(ctx, f"{base}/triggers.json", "triggers", auth)))
            sections.append(("Automations", _paged_zendesk(ctx, f"{base}/automations.json", "automations", auth)))
            sections.append(("Macros (saved replies)", _paged_zendesk(ctx, f"{base}/macros.json", "macros", auth)))
        else:
            return _result(False, f"No inventory reader for {ctx.variant}.")
    except (RuntimeError, ValueError) as e:
        return _result(False, f"{e}. Check the API key and that it has read access.")

    def active(x: dict) -> bool:
        if "active" in x:
            return bool(x["active"])
        return not x.get("deactivated_datetime")

    out = ctx.workdir / "rebuild-sheet.md"
    lines = [f"# What to rebuild in the new helpdesk ({ctx.store})", "",
             "Only active items are listed in full. Rebuild them in Commslayer (Settings > Automations and"
             " canned responses), or ask your AI assistant to do it through the Commslayer MCP and review each one.", ""]
    counts = {}
    for title, items in sections:
        live = [x for x in items if active(x)]
        counts[title] = {"total": len(items), "active": len(live)}
        lines.append(f"## {title}: {len(live)} active of {len(items)}")
        for x in live:
            name = x.get("name") or x.get("title") or f"#{x.get('id')}"
            desc = (x.get("description") or "").strip()
            lines.append(f"- **{name}**" + (f": {desc}" if desc else ""))
            logic = x.get("code") or (json.dumps(x.get("conditions"))[:600] if x.get("conditions") else "")
            if logic:
                lines.append(f"  ```\n  {logic.strip()[:600]}\n  ```")
        lines.append("")
    out.write_text("\n".join(lines))
    summary = "; ".join(f"{t}: {c['active']} active of {c['total']}" for t, c in counts.items())
    return _result(True, f"{summary}. Rebuild sheet written.",
                   {"counts": counts, "active_total": sum(c["active"] for c in counts.values())}, [str(out)])


# --- Recharge → Appstle -------------------------------------------------------------------------

MOVABLE_METHODS = ("credit_card", "card", "debit", "visa", "mastercard", "amex", "american express", "discover")
STUCK_METHODS = ("paypal", "apple_pay", "apple pay", "google_pay", "google pay", "sepa", "shop_pay_installments")


def recharge_inspect_exports(ctx: Context, inputs: dict) -> dict:
    """Count what's moving and flag subscribers whose payment method can't move."""
    headers, rows = read_csv(inputs["subscriptions_file"])
    status_col = pick(headers, "status", "subscription status", "subscription_status")
    cust_col = pick(headers, "customer_id", "customer id", "shopify_customer_id", "email", "customer email")
    if not status_col:
        return _result(False, f"No status column in {', '.join(headers[:12])}. Is this 'Subscriptions - All'?")
    by_status: dict[str, int] = {}
    active_customers: set[str] = set()
    for r in rows:
        s = (r.get(status_col) or "").strip().lower() or "unknown"
        by_status[s] = by_status.get(s, 0) + 1
        if s == "active" and cust_col:
            active_customers.add((r.get(cust_col) or "").strip().lower())
    active = by_status.get("active", 0)
    data: dict = {"subscriptions": len(rows), "active": active, "by_status": by_status,
                  "active_customers": len(active_customers)}
    lines = [f"{active:,} active subscriptions ({len(active_customers):,} customers) out of {len(rows):,} in the export."]

    pm_file = inputs.get("payment_methods_file")
    if pm_file:
        ph, prow = read_csv(pm_file)
        type_col = pick(ph, "payment_type", "payment type", "payment_method_type", "type", "processor_name", avoid=("id",))
        proc_col = pick(ph, "processor_name", "processor", "payment_processor", "gateway")
        pcust = pick(ph, "customer_id", "customer id", "shopify_customer_id", "email", "customer email")
        stuck: set[str] = set()
        processors: dict[str, int] = {}
        for r in prow:
            t = " ".join((r.get(c) or "") for c in {type_col, proc_col} if c).lower()
            if proc_col:
                p = (r.get(proc_col) or "unknown").strip().lower()
                processors[p] = processors.get(p, 0) + 1
            if any(k in t for k in STUCK_METHODS) and pcust:
                stuck.add((r.get(pcust) or "").strip().lower())
        at_risk = len(stuck & active_customers) if active_customers else len(stuck)
        data.update(processors=processors, at_risk_customers=at_risk)
        if at_risk:
            lines.append(f"{_n(at_risk, 'active customer pays', 'active customers pay')} with PayPal, Apple Pay, Google Pay or SEPA. Those methods"
                         " can't move, so plan an email asking them to re-enter a card.")
        if any("shopify" in p for p in processors):
            lines.append("Some methods sit in Shopify Payments: ask Shopify Support for the payment token export"
                         " (Shopify says it may charge for this).")
    else:
        lines.append("Add the 'Payment Methods - All' export to check which subscribers can't be moved.")
    return _result(True, " ".join(lines), data)


# --- Loyalty → BON ------------------------------------------------------------------------------

def loyalty_build_bon_csv(ctx: Context, inputs: dict) -> dict:
    """Turn the old program's customer export into BON's 3-column points file, plus a spot-check list."""
    headers, rows = read_csv(inputs["export_file"])
    email = pick(headers, "email", "customer email", "customer_email", "email address")
    points = pick(headers, "points_balance", "points balance", "current points", "available points",
                  "approved points", "approved_points", "points", "balance",
                  avoid=("pending", "spent", "earned", "lifetime", "redeemed", "expired"))
    cid = pick(headers, "shopify_customer_id", "shopify customer id", "customer id", "customer_id", "external_id",
               "external id", avoid=("loyalty", "smile", "yotpo"))
    tier = pick(headers, "vip tier", "tier", "vip_tier", "tier name", "vip")
    referral = pick(headers, "referral url", "referral_url", "referral link", "referral code", "referral")
    if not (email and points):
        return _result(False, f"Need an email and a points column; found {', '.join(headers[:15])}.")

    id_by_email: dict[str, str] = {}
    if inputs.get("shopify_customers_file"):
        sh, srows = read_csv(inputs["shopify_customers_file"])
        se, sid = pick(sh, "email"), pick(sh, "customer id", "id")
        if se and sid:
            id_by_email = {(r.get(se) or "").strip().lower(): (r.get(sid) or "").strip() for r in srows}

    out_rows, missing_id, zero = [], 0, 0
    for r in rows:
        e = (r.get(email) or "").strip()
        p = _num(r.get(points, ""))
        if not e or p is None:
            continue
        pts = math.ceil(p)
        if pts <= 0:
            zero += 1
            continue
        c = re.sub(r"\D", "", (r.get(cid) or "") if cid else "") or id_by_email.get(e.lower(), "")
        if not c:
            missing_id += 1
        out_rows.append({"Shopify Customer ID": c, "Email": e, "Points": pts})

    out = ctx.workdir / "bon-points-import.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Shopify Customer ID", "Email", "Points"])
        w.writeheader()
        w.writerows(out_rows)
    sample = random.Random(len(out_rows)).sample(out_rows, min(5, len(out_rows)))
    total = sum(r["Points"] for r in out_rows)
    lines = [f"{_n(len(out_rows), 'customer carries', 'customers carry')} {total:,} points into BON. {_n(zero, 'customer', 'customers')} with zero points left out."]
    if missing_id:
        lines.append(f"{_n(missing_id, 'row has', 'rows have')} no Shopify customer ID. Add the Shopify customers export to fill them in,"
                     " or ask BON to match on email.")
    if tier:
        lines.append("The export has VIP tiers. BON's file holds points only, so send the tier export to BON too and"
                     " set up matching tiers before launch.")
    if referral:
        lines.append("Referral links from the old program won't carry over; BON issues new ones.")
    return _result(True, " ".join(lines),
                   {"customers": len(out_rows), "points": total, "missing_id": missing_id, "zero": zero,
                    "has_tiers": bool(tier), "has_referrals": bool(referral), "spot_check": sample,
                    "spot_check_text": "; ".join(f"{r['Email']}: {r['Points']:,}" for r in sample),
                    "columns": {"email": email, "points": points, "id": cid}}, [str(out)])


# --- Storefront check ---------------------------------------------------------------------------

def storefront_check(ctx: Context, inputs: dict) -> dict:
    """Confirm on the live storefront that the new app loads and, at the end, the old one is gone."""
    from .fingerprints import Catalog
    from .scan import scan_storefront

    catalog = Catalog.load()
    scan = scan_storefront(ctx.store, catalog, pages=3, fetcher=ctx.params.get("_page_fetcher"))
    found = set(scan.detections)
    new = ctx.params.get("expect")
    old = ctx.variant if ctx.params.get("mode") == "final" else None
    ok = (not new or new in found) and (old is None or old not in found)
    parts = []
    if new:
        parts.append(f"{catalog.apps[new].name} {'is' if new in found else 'is not yet'} loading on the storefront.")
    if old:
        parts.append(f"{catalog.apps[old].name} {'still loads: remove its theme blocks or embeds' if old in found else 'is gone'}.")
    elif ctx.variant in found:
        parts.append(f"{catalog.apps[ctx.variant].name} still loads too, which is expected until you cancel it.")
    return _result(ok, " ".join(parts), {"found": sorted(found)})


ACTIONS: dict[str, Callable[[Context, dict], dict]] = {
    "reviews.inspect_export": reviews_inspect_export,
    "reviews.count_judgeme": reviews_count_judgeme,
    "helpdesk.inventory": helpdesk_inventory,
    "recharge.inspect_exports": recharge_inspect_exports,
    "loyalty.build_bon_csv": loyalty_build_bon_csv,
    "storefront.check": storefront_check,
}
