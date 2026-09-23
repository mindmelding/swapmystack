"""Turn scan, theme and bill evidence into an app inventory and ranked savings findings.

Every finding carries a confidence level and says what evidence produced it. Savings are
only counted where a bill proves the money is being spent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import __version__
from .bills import AppSpend, BillsResult, median
from .fingerprints import App, Catalog, norm
from .scan import ScanResult
from .theme import ThemeResult

CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


@dataclass
class Item:
    key: str
    name: str
    app: App | None
    category: str | None
    category_label: str
    storefront: list[dict] = field(default_factory=list)
    theme: list[dict] = field(default_factory=list)
    spend: AppSpend | None = None
    billed_active: bool = False

    @property
    def detected(self) -> bool:
        return bool(self.storefront or self.theme)

    @property
    def monthly(self) -> float:
        return self.spend.monthly if (self.spend and self.billed_active) else 0.0

    def status(self, have_bills: bool, have_scan: bool) -> str:
        if have_bills and self.billed_active and self.detected:
            return "paid, in use"
        if have_bills and self.billed_active and not self.detected:
            if self.app and (not self.app.storefront or self.app.optional):
                return "paid, back office"
            return "paid, no trace" if have_scan else "paid"
        if self.detected and have_bills and not self.billed_active:
            return "free" if (self.app and self.app.free) else "free or leftover"
        return "detected"

    def to_dict(self, have_bills: bool, have_scan: bool) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "category_label": self.category_label,
            "status": self.status(have_bills, have_scan),
            "monthly": self.monthly,
            "storefront_evidence": self.storefront,
            "theme_evidence": self.theme,
        }


@dataclass
class Finding:
    kind: str
    title: str
    detail: str
    action: str
    confidence: str
    monthly_savings: float = 0.0
    apps: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    category: str | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "category": self.category, "title": self.title, "detail": self.detail, "action": self.action,
            "confidence": self.confidence, "monthly_savings": round(self.monthly_savings, 2),
            "annual_savings": round(self.monthly_savings * 12, 2), "apps": self.apps, "evidence": self.evidence,
        }


@dataclass
class Audit:
    store: str | None
    generated_at: str
    catalog_version: str
    inventory: list[Item]
    findings: list[Finding]
    scan: ScanResult | None
    theme: ThemeResult | None
    bills: BillsResult | None
    unknown_handles: list[str]
    unmatched_hosts: dict[str, int]

    @property
    def have_bills(self) -> bool:
        return self.bills is not None

    @property
    def have_scan(self) -> bool:
        return self.scan is not None or self.theme is not None

    def savings(self, *levels: str) -> float:
        return round(sum(f.monthly_savings for f in self.findings if f.confidence in levels), 2)

    @property
    def total_monthly_spend(self) -> float:
        return round(sum(i.monthly for i in self.inventory), 2)

    def to_dict(self) -> dict:
        return {
            "tool": f"appspend {__version__}",
            "store": self.store,
            "generated_at": self.generated_at,
            "catalog_version": self.catalog_version,
            "summary": {
                "apps_found": len(self.inventory),
                "monthly_app_spend": self.total_monthly_spend if self.have_bills else None,
                "savings_monthly_confirmed": self.savings("high", "medium"),
                "savings_monthly_possible": self.savings("low"),
                "savings_annual_confirmed": round(self.savings("high", "medium") * 12, 2),
            },
            "findings": [f.to_dict() for f in self.findings],
            "inventory": [i.to_dict(self.have_bills, self.have_scan) for i in self.inventory],
            "unknown_app_handles": self.unknown_handles,
            "unmatched_script_hosts": self.unmatched_hosts,
            "inputs": {
                "storefront": self.scan.to_dict() if self.scan else None,
                "theme": self.theme.to_dict() if self.theme else None,
                "bills": self.bills.to_dict() if self.bills else None,
            },
        }


def _money(v: float) -> str:
    return f"${v:,.0f}" if v >= 100 or v == int(v) else f"${v:,.2f}"


def build_inventory(catalog: Catalog, scan: ScanResult | None, theme: ThemeResult | None,
                    bills: BillsResult | None) -> list[Item]:
    items: dict[str, Item] = {}

    def get(key: str, app: App | None, name: str) -> Item:
        if key not in items:
            cat = catalog.category(app)
            items[key] = Item(key=key, name=app.name if app else name, app=app,
                              category=cat.id if cat else None,
                              category_label=cat.label if cat else "Unrecognized")
        return items[key]

    if scan:
        for app_id, evs in scan.detections.items():
            get(app_id, catalog.apps[app_id], app_id).storefront = [e.to_dict() for e in evs]
    if theme:
        for app_id, evs in theme.detections.items():
            get(app_id, catalog.apps[app_id], app_id).theme = [e.to_dict() for e in evs]
    if bills:
        for key, spend in bills.apps.items():
            app = catalog.apps.get(spend.app_id) if spend.app_id else None
            item = get(key, app, spend.name)
            item.spend = spend
            item.billed_active = bills.active(spend)

    # Bills for apps the catalog doesn't know can still be tied to an unrecognized handle on the page,
    # e.g. "Order Limits Magic" on the bill and "order-limits-magic" in an app block.
    handles = {norm(h): h for h in (list(scan.unknown_handles) if scan else []) + (theme.unknown_handles if theme else [])}
    for it in items.values():
        if it.app or not it.spend:
            continue
        name = norm(it.name)
        for nh, h in handles.items():
            if len(name) >= 5 and (name in nh or nh in name):
                ev = {"source": "app-block", "detail": h, "page": ""}
                (it.storefront if scan and h in scan.unknown_handles else it.theme).append(ev)
                break

    return sorted(items.values(), key=lambda i: (-i.monthly, i.category_label, i.name))


def analyze(catalog: Catalog, scan: ScanResult | None = None, theme: ThemeResult | None = None,
            bills: BillsResult | None = None, pages_checked: int = 0) -> Audit:
    inventory = build_inventory(catalog, scan, theme, bills)
    findings: list[Finding] = []
    have_bills = bills is not None
    have_scan = scan is not None or theme is not None

    # 1. Paid apps that leave no trace on the storefront or in the theme.
    if have_bills and have_scan:
        for it in inventory:
            if not it.billed_active or it.detected or it.monthly <= 0:
                continue
            if it.app and (not it.app.storefront or it.app.optional):
                continue  # back-office app, or one that often runs without storefront code
            where = []
            if scan:
                where.append(f"{pages_checked or len(scan.pages)} storefront pages")
            if theme:
                where.append("the theme files")
            # Many apps load through ScriptTags that never touch theme files, so a theme alone proves little.
            conf = "high" if (scan and theme and it.app) else "medium" if (scan and it.app) else "low"
            findings.append(Finding(
                kind="paid_no_trace",
                title=(f"{it.name} is billed but not running on your store" if scan
                       else f"{it.name} is billed but absent from your theme"),
                detail=(f"You pay {_money(it.monthly)}/mo, and none of its code appeared in "
                        f"{' or '.join(where)}. Either it was never set up, it was switched off, "
                        f"or it only loads on a page we did not check."
                        + ("" if it.app else " appspend doesn't recognize this app yet, so treat this as a lead to check.")),
                action=f"Ask who uses {it.name}. If nobody does, uninstall it in Settings > Apps.",
                confidence=conf, monthly_savings=it.monthly, apps=[it.name],
            ))

    # 2. Two or more apps doing the same job.
    by_cat: dict[str, list[Item]] = {}
    for it in inventory:
        if not it.category:
            continue
        cat = catalog.categories[it.category]
        if not cat.exclusive:
            continue
        live = it.billed_active if have_bills else it.detected
        if live and not (it.app and it.app.free):
            by_cat.setdefault(it.category, []).append(it)
    for cid, group in by_cat.items():
        if len(group) < 2:
            continue
        cat = catalog.categories[cid]
        names = [g.name for g in group]
        if have_bills:
            costs = sorted((g.monthly for g in group), reverse=True)
            saving = round(sum(costs[1:]), 2)  # keep the priciest, assume it's the one you chose
            conf = "medium" if saving > 0 else "info"
            money = f" Together they cost {_money(sum(costs))}/mo."
        else:
            saving, conf, money = 0.0, "low", ""
        findings.append(Finding(
            kind="overlap",
            title=f"{len(group)} {cat.label.lower()} apps doing one job",
            detail=f"{', '.join(names[:-1])} and {names[-1]} are {'both' if len(names) == 2 else 'all'} running.{money} Stores rarely need more than one.",
            action=f"Pick one. Savings assume you keep the most expensive; keeping a cheaper one saves more.",
            confidence=conf, monthly_savings=saving, apps=names, category=cid,
        ))

    # 3. Code on the storefront from apps you no longer pay for.
    if have_bills:
        for it in inventory:
            if it.detected and not it.billed_active and not (it.app and it.app.free) and it.app:
                n = len(it.storefront) + len(it.theme)
                findings.append(Finding(
                    kind="leftover_code",
                    title=f"{it.name} code is live but not on your bills",
                    detail=(f"We found {n} trace{'s' if n != 1 else ''} of {it.name} but no charge in the export. "
                            f"It is on a free plan, billed somewhere else, or uninstalled with its code left behind. "
                            f"Leftover scripts slow every page."),
                    action=f"If you don't use {it.name}, remove its app embed and snippets from the theme.",
                    confidence="medium", apps=[it.name],
                    evidence=[e["detail"] for e in (it.storefront + it.theme)[:4]],
                ))

    # 4. Theme leftovers: orphan snippets and disabled app embeds.
    if theme:
        orphan_apps = [o for o in theme.orphan_snippets if o["app_id"]]
        if orphan_apps:
            names = sorted({catalog.apps[o["app_id"]].name for o in orphan_apps})
            size = sum(o["bytes"] for o in orphan_apps)
            size_txt = f"{size / 1024:.0f} KB" if size >= 1024 else f"{size} bytes"
            findings.append(Finding(
                kind="theme_leftovers",
                title=f"{len(orphan_apps)} app snippet{'s' if len(orphan_apps) != 1 else ''} left behind in the theme",
                detail=(f"Snippets from {', '.join(names)} ({size_txt}) are no longer rendered anywhere. "
                        f"This is what uninstalled apps usually leave behind."),
                action="Delete them in a duplicate theme, preview, then publish.",
                confidence="medium", apps=names, evidence=[o["name"] for o in orphan_apps[:8]],
            ))
        disabled = [e for e in theme.app_embeds if e["disabled"]]
        if disabled:
            findings.append(Finding(
                kind="disabled_embeds",
                title=f"{len(disabled)} app embed{'s' if len(disabled) != 1 else ''} switched off but still installed",
                detail="These apps are installed but their theme embed is disabled. If they are paid, you pay for nothing.",
                action="Uninstall any you no longer plan to switch back on.",
                confidence="info", apps=sorted({(catalog.apps[e['app_id']].name if e['app_id'] else e['handle']) for e in disabled}),
            ))

    # 5. Price creep and usage spikes, from bill history.
    if bills:
        for it in inventory:
            if not it.spend or not it.billed_active:
                continue
            rec = it.spend.recurring_series()
            if len(rec) >= 3:
                first, last = rec[0][1], rec[-1][1]
                if first > 0 and last >= first * 1.15 and last - first >= 10:
                    findings.append(Finding(
                        kind="price_creep",
                        title=f"{it.name} went from {_money(first)} to {_money(last)} a month",
                        detail=(f"A {((last / first) - 1) * 100:.0f}% rise between {rec[0][0]} and {rec[-1][0]}. "
                                f"Plan changes often happen through usage tiers nobody chose."),
                        action="Check which plan you're on against what you use. Ask for the old rate or an annual price.",
                        confidence="info", apps=[it.name],
                    ))
            usage = [v for _, v in it.spend.usage_series()]
            if len(usage) >= 3 and usage[-1] >= 25:
                base = median(usage[:-1])
                if base > 0 and usage[-1] >= base * 1.5:
                    findings.append(Finding(
                        kind="usage_spike",
                        title=f"{it.name} usage fees jumped to {_money(usage[-1])}",
                        detail=f"Last month's usage charges were {usage[-1] / base:.1f}x the usual {_money(base)}.",
                        action="Find what drove the usage. Metered apps often have caps you can set.",
                        confidence="info", apps=[it.name],
                    ))

    # 6. Paid apps where Shopify or a vendor offers the core job free.
    flagged = {a for f in findings if f.kind in ("paid_no_trace", "overlap") for a in f.apps}
    if have_bills:
        for it in inventory:
            if not it.category or it.name in flagged or it.monthly < 20:
                continue
            free = catalog.categories[it.category].free_option
            if free:
                findings.append(Finding(
                    kind="free_option",
                    title=f"A free option exists for {it.name}",
                    detail=f"You pay {_money(it.monthly)}/mo. {free} It may not match every feature you use.",
                    action="Compare the features you use against the free option before your next renewal.",
                    confidence="low", monthly_savings=it.monthly, apps=[it.name],
                ))

    findings.sort(key=lambda f: (CONFIDENCE_ORDER[f.confidence], -f.monthly_savings))

    claimed = {e["detail"] for it in inventory if not it.app for e in it.storefront + it.theme}
    unknown = sorted((set(scan.unknown_handles) if scan else set()) | set(theme.unknown_handles if theme else []) - claimed)
    hosts = dict(sorted((scan.third_party_hosts if scan else {}).items(), key=lambda kv: -kv[1]))
    return Audit(
        store=scan.store if scan else None,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        catalog_version=catalog.version,
        inventory=inventory, findings=findings, scan=scan, theme=theme, bills=bills,
        unknown_handles=unknown, unmatched_hosts=hosts,
    )
