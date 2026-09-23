"""Cheaper paths: researched replacements, downgrades and negotiation notes for common paid apps.

Every price and feature claim in data/alternatives.json carries a source URL and the date it was
checked. Entries older than STALE_DAYS are shown with a recheck warning, because app pricing moves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path

STALE_DAYS = 90
SWITCH_ORDER = {"low": 0, "medium": 1, "high": 2}
STRATEGIES = {"replace", "downgrade", "negotiate", "remove"}


@dataclass(frozen=True)
class Price:
    low: float | None
    high: float | None
    unit: str
    note: str

    @classmethod
    def from_dict(cls, d: dict | None) -> "Price":
        d = d or {}
        return cls(d.get("low"), d.get("high"), d.get("unit") or "month", d.get("note") or "")

    def label(self) -> str:
        if self.low is None and self.high is None:
            return self.note or "price not published"
        if self.low == 0 and not self.high:
            return "free"
        per = "/mo" if self.unit == "month" else f" {self.unit}"
        if self.high and self.high != self.low:
            return f"${self.low:,.0f}–${self.high:,.0f}{per}"
        return f"${self.low:,.0f}{per}"


@dataclass(frozen=True)
class Alternative:
    name: str
    kind: str
    price: Price
    source: str
    checked: str
    covers: tuple[str, ...]
    misses: tuple[str, ...]
    rating: str


@dataclass(frozen=True)
class Path_:
    app: str
    incumbent_price: Price
    incumbent_source: str
    checked: str
    switch_cost: str
    switch_note: str
    strategy: str
    alternatives: tuple[Alternative, ...]

    def stale(self, today: date | None = None) -> bool:
        try:
            checked = date.fromisoformat(self.checked)
        except ValueError:
            return True
        return ((today or date.today()) - checked).days > STALE_DAYS

    @property
    def best(self) -> Alternative | None:
        return self.alternatives[0] if self.alternatives else None


def _load_entries(data: dict) -> dict[str, Path_]:
    out: dict[str, Path_] = {}
    for e in data.get("entries", []):
        ip = e.get("incumbent_price") or {}
        strategy = e.get("strategy", "replace")
        if strategy not in STRATEGIES:
            raise ValueError(f"{e.get('app')}: unknown strategy {strategy}")
        if e.get("switch_cost") not in SWITCH_ORDER:
            raise ValueError(f"{e.get('app')}: switch_cost must be low, medium or high")
        alts = tuple(
            Alternative(
                name=a["name"], kind=a.get("kind", ""), price=Price.from_dict(a.get("price")),
                source=a.get("source", ""), checked=a.get("checked", ""),
                covers=tuple(a.get("covers") or ()), misses=tuple(a.get("misses") or ()),
                rating=a.get("app_store_rating") or "",
            )
            for a in e.get("alternatives", [])
        )
        out[e["app"]] = Path_(
            app=e["app"], incumbent_price=Price.from_dict(ip), incumbent_source=ip.get("source", ""),
            checked=ip.get("checked", ""), switch_cost=e["switch_cost"], switch_note=e.get("switch_note", ""),
            strategy=strategy, alternatives=alts,
        )
    return out


def load(path: str | Path | None = None) -> dict[str, Path_]:
    if path:
        return _load_entries(json.loads(Path(path).read_text()))
    f = resources.files("appspend").joinpath("data/alternatives.json")
    return _load_entries(json.loads(f.read_text())) if f.is_file() else {}
