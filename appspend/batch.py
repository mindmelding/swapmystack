"""Batch mode: scan a list of storefronts politely and rank them by what an audit would likely find.

Storefront-only, so there are no dollar figures. What it can show from the outside is how many
paid apps a store runs and where two apps do the same job. Those are the stores worth an audit.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .analyze import analyze
from .fingerprints import Catalog
from .scan import Fetcher, normalize_store, scan_storefront

NOT_PAID = {"ad_channels"}  # free sales-channel pixels say nothing about app spend

OUT_FIELDS = [
    "domain", "name", "status", "shopify", "paid_apps", "overlaps", "score",
    "pitch", "apps", "unrecognized", "pages_read",
]


@dataclass
class Row:
    domain: str
    name: str = ""
    extra: dict = field(default_factory=dict)
    status: str = ""
    shopify: bool = False
    paid_apps: list[str] = field(default_factory=list)
    overlaps: list[str] = field(default_factory=list)
    all_apps: list[str] = field(default_factory=list)
    unrecognized: list[str] = field(default_factory=list)
    pages_read: int = 0

    @property
    def score(self) -> int:
        """Overlaps are the strongest outside signal of waste; paid app count sets the size of the prize."""
        return len(self.overlaps) * 5 + len(self.paid_apps)

    @property
    def pitch(self) -> str:
        if self.overlaps:
            return f"Runs {self.overlaps[0]}. Most stores keep one."
        if len(self.paid_apps) >= 8:
            return f"Runs {len(self.paid_apps)} third-party apps. Worth a bills check."
        return ""

    def to_csv(self) -> dict:
        return {
            "domain": self.domain, "name": self.name, "status": self.status,
            "shopify": "yes" if self.shopify else "no", "paid_apps": len(self.paid_apps),
            "overlaps": " | ".join(self.overlaps), "score": self.score, "pitch": self.pitch,
            "apps": "; ".join(self.all_apps), "unrecognized": "; ".join(self.unrecognized),
            "pages_read": self.pages_read, **self.extra,
        }


def read_domains(path: str | Path) -> list[Row]:
    """A .txt with one domain per line, or a CSV with a 'domain' column (other columns are carried through)."""
    path = Path(path).expanduser()
    text = path.read_text(encoding="utf-8-sig")
    rows: list[Row] = []
    seen: set[str] = set()
    first = text.splitlines()[0].lower() if text.strip() else ""
    if "domain" in first and "," in first:
        for r in csv.DictReader(text.splitlines()):
            d = (r.pop("domain", "") or "").strip()
            name = (r.pop("name", "") or "").strip()
            if d and not d.startswith("#"):
                key = normalize_store(d)
                if key not in seen:
                    seen.add(key)
                    rows.append(Row(domain=d, name=name, extra={k: v for k, v in r.items() if k and k not in OUT_FIELDS}))
    else:
        for line in text.splitlines():
            d = line.split("#")[0].strip()
            if d and normalize_store(d) not in seen:
                seen.add(normalize_store(d))
                rows.append(Row(domain=d))
    return rows


def scan_one(row: Row, catalog: Catalog, pages: int, fetcher: Fetcher | None) -> Row:
    scan = scan_storefront(row.domain, catalog, pages=pages, fetcher=fetcher)
    row.pages_read = sum(1 for p in scan.pages if p["status"] == 200)
    if not row.pages_read:
        first = scan.pages[0] if scan.pages else {}
        row.status = f"unreachable ({first.get('error') or first.get('status')})"
        return row
    row.shopify = scan.is_shopify
    row.status = "ok" if scan.is_shopify else "not shopify"
    if not scan.is_shopify:
        return row
    audit = analyze(catalog, scan)
    for it in audit.inventory:
        row.all_apps.append(it.name)
        if it.app and not it.app.free and it.category not in NOT_PAID:
            row.paid_apps.append(it.name)
    row.overlaps = [f"{' + '.join(f.apps)} ({catalog.categories[f.category].label.lower()})"
                    for f in audit.findings if f.kind == "overlap" and f.category]
    row.unrecognized = audit.unknown_handles
    return row


def run_batch(rows: list[Row], catalog: Catalog, pages: int = 2, delay: float = 1.0,
              fetcher: Fetcher | None = None, progress: Callable[[int, int, Row], None] | None = None) -> list[Row]:
    for i, row in enumerate(rows, 1):
        scan_one(row, catalog, pages, fetcher)
        if progress:
            progress(i, len(rows), row)
        if delay and i < len(rows):
            time.sleep(delay)
    return sorted(rows, key=lambda r: (-r.score, r.domain))


def write_csv(rows: list[Row], path: str | Path) -> None:
    extra_cols = sorted({k for r in rows for k in r.extra})
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS + extra_cols)
        w.writeheader()
        for r in rows:
            w.writerow(r.to_csv())
