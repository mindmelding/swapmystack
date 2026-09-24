"""Bill parsing: turn a Shopify bills export (or a simple app,monthly_cost sheet) into per-app spend.

Shopify emails the export from Settings > Billing > Export bills. Its columns are not documented
and have changed over time, so columns are found by name, not position. Any CSV with a
description-like column and an amount-like column works.
"""

from __future__ import annotations

import csv
import io
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .fingerprints import Catalog, norm

NAME_HINTS = ("app", "app name", "application", "description", "item", "title", "name", "charge", "details", "source")
AMOUNT_HINTS = ("amount", "total", "price", "cost", "monthly", "charge amount", "subtotal")
DATE_HINTS = ("date", "billed", "period start", "start", "created", "issued")
KIND_HINTS = ("type", "category", "kind", "charge type", "charge category")

# Rows that are Shopify's own fees, not app spend.
NOT_APPS = (
    "transaction fee", "shipping label", "label", "sales tax", " tax", "vat", "gst", "hst",
    "shopify plan", "subscription plan", "basic shopify", "advanced shopify", "shopify plus",
    "grow plan", "basic plan", "advanced plan", "domain", "theme purchase", "pos pro",
    "shopify payments", "chargeback", "shopify capital", "shopify email", "markets pro",
)

DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y")


@dataclass
class Charge:
    day: date | None
    name: str
    amount: float
    kind: str  # "recurring", "usage" or "one_time"


@dataclass
class AppSpend:
    key: str
    name: str
    app_id: str | None
    months: dict[str, dict[str, float]] = field(default_factory=dict)  # "YYYY-MM" -> {recurring, usage, one_time}
    last_billed: date | None = None
    manual_monthly: float | None = None

    def recurring_series(self) -> list[tuple[str, float]]:
        return [(m, v["recurring"]) for m, v in sorted(self.months.items()) if v["recurring"] > 0]

    def usage_series(self) -> list[tuple[str, float]]:
        return [(m, v["usage"]) for m, v in sorted(self.months.items())]

    @property
    def monthly(self) -> float:
        """Current run rate: latest recurring charge plus average usage over the last three months."""
        if self.manual_monthly is not None:
            return self.manual_monthly
        rec = self.recurring_series()
        latest = rec[-1][1] if rec else 0.0
        usage = [v for _, v in self.usage_series()][-3:]
        return round(latest + (sum(usage) / len(usage) if usage else 0.0), 2)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "app_id": self.app_id,
            "monthly": self.monthly,
            "last_billed": self.last_billed.isoformat() if self.last_billed else None,
            "months": self.months,
        }


@dataclass
class BillsResult:
    source: str
    format: str                       # "shopify-export" or "manual"
    columns: dict[str, str | None]
    as_of: date | None
    apps: dict[str, AppSpend] = field(default_factory=dict)
    skipped_rows: int = 0

    def active(self, spend: AppSpend, window_days: int = 25) -> bool:
        """Billed in the most recent cycle of the export. Shopify puts all app charges on one bill cycle."""
        if spend.manual_monthly is not None:
            return spend.manual_monthly > 0
        if not spend.last_billed or not self.as_of:
            return bool(spend.months)
        return (self.as_of - spend.last_billed).days <= window_days and spend.monthly > 0

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "format": self.format,
            "columns": self.columns,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "skipped_rows": self.skipped_rows,
            "apps": {k: v.to_dict() for k, v in self.apps.items()},
        }


def parse_amount(raw: str) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = re.sub(r"[^0-9.,]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{2}$", s) else s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if negative else v


def parse_date(raw: str) -> date | None:
    s = (raw or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return date.fromisoformat(m.group(1))
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _pick(headers: list[str], hints: tuple[str, ...], exclude: set[str]) -> str | None:
    lowered = {h: h.strip().lower() for h in headers if h and h not in exclude}
    for hint in hints:  # exact match first, in priority order
        for h, low in lowered.items():
            if low == hint:
                return h
    for hint in hints:
        for h, low in lowered.items():
            if hint in low:
                return h
    return None


def _kind(kind_text: str, name: str) -> str:
    t = f"{kind_text} {name}".lower()
    if "usage" in t or "metered" in t:
        return "usage"
    if "one-time" in t or "one time" in t or "onetime" in t or "setup" in t:
        return "one_time"
    return "recurring"


def _is_app_row(kind_text: str, name: str, catalog: Catalog) -> bool:
    k = kind_text.lower()
    if k and "app" in k:
        return True
    if catalog.match_key(name):
        return True
    low = f" {name.lower()} "
    if any(x in low or x in k for x in NOT_APPS):
        return False
    return not k  # no category column: keep unknown rows, they show up for review


def _clean_name(name: str) -> str:
    # "Judge.me Product Reviews - Awesome plan (Sep 1 - Sep 30)" -> "Judge.me Product Reviews"
    s = re.sub(r"\(.*?\)", "", name)
    s = re.split(r"\s[-–—|:]\s", s)[0]
    return s.strip() or name.strip()


def parse_bills(path: str | Path, catalog: Catalog) -> BillsResult:
    path = Path(path).expanduser()
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h]

    name_col = _pick(headers, NAME_HINTS, set())
    amount_col = _pick(headers, AMOUNT_HINTS, {name_col} if name_col else set())
    date_col = _pick(headers, DATE_HINTS, {c for c in (name_col, amount_col) if c})
    kind_col = _pick(headers, KIND_HINTS, {c for c in (name_col, amount_col, date_col) if c})
    if not name_col or not amount_col:
        raise ValueError(
            f"Could not find a name column and an amount column in {path.name}. "
            f"Headers seen: {headers}. A two-column CSV 'app,monthly_cost' also works."
        )

    manual = date_col is None
    result = BillsResult(
        source=str(path),
        format="manual" if manual else "shopify-export",
        columns={"name": name_col, "amount": amount_col, "date": date_col, "kind": kind_col},
        as_of=None,
    )

    charges: list[Charge] = []
    for row in reader:
        raw_name = (row.get(name_col) or "").strip()
        amount = parse_amount(row.get(amount_col) or "")
        kind_text = (row.get(kind_col) or "") if kind_col else ""
        if not raw_name or amount is None or not _is_app_row(kind_text, raw_name, catalog):
            result.skipped_rows += 1
            continue
        day = parse_date(row.get(date_col) or "") if date_col else None
        charges.append(Charge(day, raw_name, amount, _kind(kind_text, raw_name)))

    days = [c.day for c in charges if c.day]
    result.as_of = max(days) if days else None

    for c in charges:
        app = catalog.match_key(c.name)
        key = app.id if app else norm(_clean_name(c.name))
        spend = result.apps.get(key)
        if spend is None:
            spend = result.apps[key] = AppSpend(key=key, name=app.name if app else _clean_name(c.name), app_id=app.id if app else None)
        if manual:
            spend.manual_monthly = round((spend.manual_monthly or 0.0) + c.amount, 2)
            continue
        month = c.day.strftime("%Y-%m") if c.day else "unknown"
        bucket = spend.months.setdefault(month, {"recurring": 0.0, "usage": 0.0, "one_time": 0.0})
        bucket[c.kind] = round(bucket[c.kind] + c.amount, 2)
        if c.day and (spend.last_billed is None or c.day > spend.last_billed):
            spend.last_billed = c.day

    return result


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0
