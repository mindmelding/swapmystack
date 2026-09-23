"""Render an Audit as terminal text, Markdown, JSON or a self-contained HTML report."""

from __future__ import annotations

import html
import json
import os
import sys

from .analyze import Audit, Finding

CONF_LABEL = {"high": "High confidence", "medium": "Likely", "low": "Worth checking", "info": "For your records"}


def _m(v: float) -> str:
    return f"${v:,.0f}"


def _host(audit: Audit) -> str:
    return (audit.store or "your store").replace("https://", "")


def _inputs_line(audit: Audit) -> str:
    parts = []
    if audit.scan:
        ok = sum(1 for p in audit.scan.pages if p["status"] == 200)
        parts.append(f"{ok} storefront page{'s' if ok != 1 else ''}")
    if audit.theme:
        parts.append(f"theme ({audit.theme.files} files)")
    if audit.bills:
        parts.append(f"bills ({len(audit.bills.apps)} apps" + (f", through {audit.bills.as_of}" if audit.bills.as_of else "") + ")")
    return ", ".join(parts) or "no inputs"


# ---------------------------------------------------------------- terminal

def render_text(audit: Audit, color: bool | None = None) -> str:
    if color is None:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    b = (lambda s: f"\033[1m{s}\033[0m") if color else (lambda s: s)
    d = (lambda s: f"\033[2m{s}\033[0m") if color else (lambda s: s)
    r = (lambda s: f"\033[31m{s}\033[0m") if color else (lambda s: s)

    out = [b(f"appspend · {_host(audit)}"), d(f"read: {_inputs_line(audit)}"), ""]
    if audit.have_bills:
        conf = audit.savings("high", "medium")
        out.append(f"App spend      {_m(audit.total_monthly_spend)}/mo")
        out.append(f"Likely savings {r(_m(conf) + '/mo')}  ({_m(conf * 12)}/yr)")
        low = audit.savings("low")
        if low:
            out.append(d(f"Worth checking {_m(low)}/mo more"))
    else:
        out.append(f"{len(audit.inventory)} apps found. Add --bills to put dollar figures on the findings.")
    out.append("")

    if audit.findings:
        out.append(b("Findings"))
        for n, f in enumerate(audit.findings, 1):
            money = f"  {_m(f.monthly_savings)}/mo" if f.monthly_savings else ""
            out.append(f"{n:>2}. {f.title}{r(money)}  {d('[' + CONF_LABEL[f.confidence] + ']')}")
            out.append(d(f"    {f.action}"))
        out.append("")

    out.append(b("Apps"))
    for it in audit.inventory:
        status = it.status(audit.have_bills, audit.have_scan)
        money = f"{_m(it.monthly)}/mo" if it.monthly else ""
        out.append(f"  {it.name:<28} {it.category_label:<28} {status:<18} {money}")
    if audit.unknown_handles:
        out.append(d(f"\nUnrecognized app handles: {', '.join(audit.unknown_handles)}"))
    return "\n".join(out)


# ---------------------------------------------------------------- markdown

def render_markdown(audit: Audit) -> str:
    lines = [f"# App Spend Audit: {_host(audit)}", "", f"_Read: {_inputs_line(audit)}. Generated {audit.generated_at}._", ""]
    if audit.have_bills:
        conf = audit.savings("high", "medium")
        lines += [f"**Likely savings: {_m(conf)}/mo ({_m(conf * 12)}/yr)** out of {_m(audit.total_monthly_spend)}/mo in app spend.", ""]
    lines += ["## Findings", ""]
    for n, f in enumerate(audit.findings, 1):
        money = f" · {_m(f.monthly_savings)}/mo" if f.monthly_savings else ""
        lines += [f"{n}. **{f.title}**{money} _({CONF_LABEL[f.confidence]})_  ", f"   {f.detail}  ", f"   → {f.action}", ""]
    lines += ["## Apps", "", "| App | Category | Status | Monthly |", "|---|---|---|---|"]
    for it in audit.inventory:
        lines.append(f"| {it.name} | {it.category_label} | {it.status(audit.have_bills, audit.have_scan)} | {_m(it.monthly) if it.monthly else ''} |")
    if audit.unknown_handles:
        lines += ["", f"Unrecognized app handles: {', '.join(audit.unknown_handles)}"]
    return "\n".join(lines) + "\n"


def render_json(audit: Audit) -> str:
    return json.dumps(audit.to_dict(), indent=2, default=str)


# ---------------------------------------------------------------- html

CSS = """
:root{
  --paper:#F4EFE4;--paper-2:#EBE4D5;--ink:#1C1814;--ink-2:#4A4238;--ink-3:#7A7064;
  --rule:#CFC5B3;--accent:#7A2E22;--accent-soft:#E9D9D0;
  --serif:"Tiempos Text","Iowan Old Style","Charter","Source Serif 4",Georgia,serif;
  --display:"Tiempos Headline","GT Sectra","Iowan Old Style","Charter",Georgia,serif;
  --mono:"Berkeley Mono","JetBrains Mono","SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#171411;--paper-2:#211C17;--ink:#EDE6D8;--ink-2:#C4B9A7;--ink-3:#8F8574;
  --rule:#3A332B;--accent:#D58B72;--accent-soft:#3A2520;}}
:root[data-theme="dark"]{--paper:#171411;--paper-2:#211C17;--ink:#EDE6D8;--ink-2:#C4B9A7;--ink-3:#8F8574;--rule:#3A332B;--accent:#D58B72;--accent-soft:#3A2520;}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:18px/1.55 var(--serif);font-feature-settings:"onum","kern"}
.page{max-width:1120px;padding:72px 16px 96px clamp(16px,9vw,128px)}
.eyebrow,.meta,.num,.label,td.mono,.caption{font-family:var(--mono);font-size:12px;letter-spacing:.04em;color:var(--ink-3)}
.eyebrow{text-transform:uppercase;letter-spacing:.14em}
h1{font:400 clamp(40px,7.5vw,96px)/1.0 var(--display);letter-spacing:-.02em;margin:18px 0 14px;word-break:break-word}
.meta{margin:0 0 72px}
.lede{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;border-top:1px solid var(--ink);padding-top:28px;margin-bottom:88px;max-width:760px}
.lede .figure{font:400 clamp(56px,10vw,128px)/0.95 var(--display);letter-spacing:-.03em;color:var(--accent);font-variant-numeric:lining-nums}
.lede p{margin:6px 0 0;font-size:21px;color:var(--ink-2);max-width:34em}
.lede .sub{font-size:16px;color:var(--ink-3)}
section{margin-bottom:88px}
h2{font:400 13px/1 var(--mono);text-transform:uppercase;letter-spacing:.16em;color:var(--ink-3);margin:0 0 8px;padding-bottom:14px;border-bottom:1px solid var(--rule)}
ol.findings{list-style:none;margin:0;padding:0;counter-reset:f}
ol.findings li{counter-increment:f;display:grid;grid-template-columns:48px minmax(0,1fr) 150px;gap:0 24px;padding:26px 0;border-bottom:1px solid var(--rule)}
ol.findings li::before{content:counter(f,decimal-leading-zero);font:12px/2.1 var(--mono);color:var(--ink-3)}
.f-title{font:400 24px/1.25 var(--display);letter-spacing:-.01em;margin:0 0 8px}
.f-detail{margin:0 0 10px;color:var(--ink-2);max-width:40em}
.f-action{margin:0;font-style:italic;max-width:40em}
.f-evidence{margin:10px 0 0;font:12px/1.6 var(--mono);color:var(--ink-3);word-break:break-all}
.f-side{text-align:right}
.f-side .amt{font:400 28px/1.1 var(--display);color:var(--ink);font-variant-numeric:lining-nums}
.f-side .per{font:12px var(--mono);color:var(--ink-3)}
.pill{display:inline-block;margin-top:10px;padding:2px 10px;border-radius:9999px;font:11px/1.6 var(--mono);letter-spacing:.06em;border:1px solid var(--rule);color:var(--ink-2)}
.pill.high,.pill.medium{border-color:var(--accent);color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:16px}
th{font:400 11px var(--mono);text-transform:uppercase;letter-spacing:.12em;color:var(--ink-3);text-align:left;padding:12px 12px 12px 0;border-bottom:1px solid var(--rule)}
td{padding:12px 12px 12px 0;border-bottom:1px solid var(--rule);vertical-align:top}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums lining-nums}
td.mono{font-size:11px;word-break:break-all}
.status{font:12px var(--mono);color:var(--ink-2)}
.status.warn{color:var(--accent)}
.notes{columns:2 320px;column-gap:48px;font-size:15px;color:var(--ink-2)}
.notes p{margin:0 0 14px;break-inside:avoid}
.caption{display:block;margin-top:12px}
.empty{color:var(--ink-3);font-style:italic}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
@media (max-width:720px){
  body{font-size:17px}
  .page{padding-top:40px}
  ol.findings li{grid-template-columns:minmax(0,1fr);gap:4px}
  ol.findings li::before{line-height:1.4}
  .f-side{text-align:left}
  .hide-sm{display:none}
}
@media print{body{background:#fff}.page{padding:0}section{break-inside:avoid-page}}
"""

STATUS_WARN = {"paid, no trace", "free or leftover"}


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _finding_html(f: Finding) -> str:
    side = ""
    if f.monthly_savings:
        side = f'<div class="amt">{_m(f.monthly_savings)}</div><div class="per">per month · {_m(f.monthly_savings * 12)}/yr</div>'
    ev = f'<p class="f-evidence">{" · ".join(_e(x) for x in f.evidence)}</p>' if f.evidence else ""
    return (
        f'<li><div><h3 class="f-title">{_e(f.title)}</h3>'
        f'<p class="f-detail">{_e(f.detail)}</p><p class="f-action">{_e(f.action)}</p>{ev}</div>'
        f'<div class="f-side">{side}<span class="pill {f.confidence}">{_e(CONF_LABEL[f.confidence])}</span></div></li>'
    )


def render_html(audit: Audit) -> str:
    host = _host(audit)
    if audit.have_bills:
        conf = audit.savings("high", "medium")
        low = audit.savings("low")
        lede = (
            f'<div class="figure">{_m(conf * 12)}</div>'
            f'<p>a year in app spend you can likely stop paying, from {_m(audit.total_monthly_spend)} a month across {sum(1 for i in audit.inventory if i.monthly)} paid apps.</p>'
            + (f'<p class="sub">Another {_m(low * 12)} a year is worth checking against free options.</p>' if low else "")
        )
    else:
        lede = (
            f'<div class="figure">{len(audit.inventory)}</div>'
            f'<p>apps running on {_e(host)}. Add your Shopify bills export to see which ones you pay for and what you can drop.</p>'
        )

    findings = "".join(_finding_html(f) for f in audit.findings) or '<li><p class="empty">Nothing to trim. This stack is clean.</p></li>'

    rows = []
    for it in audit.inventory:
        status = it.status(audit.have_bills, audit.have_scan)
        ev = (it.storefront + it.theme)[:2]
        ev_txt = " · ".join(e["detail"] for e in ev)
        rows.append(
            f'<tr><td>{_e(it.name)}</td><td class="hide-sm">{_e(it.category_label)}</td>'
            f'<td><span class="status{" warn" if status in STATUS_WARN else ""}">{_e(status)}</span></td>'
            f'<td class="num">{_m(it.monthly) if it.monthly else "—"}</td>'
            f'<td class="mono hide-sm">{_e(ev_txt)}</td></tr>'
        )
    inventory = (
        '<div class="tablewrap"><table><thead><tr><th>App</th><th class="hide-sm">Job</th><th>Status</th>'
        '<th class="num">Monthly</th><th class="hide-sm">Evidence</th></tr></thead><tbody>'
        + "".join(rows) + "</tbody></table></div>"
    )

    unknown = ""
    if audit.unknown_handles or audit.unmatched_hosts:
        bits = []
        if audit.unknown_handles:
            bits.append(f"<p>App handles on the page that appspend can't name yet: {_e(', '.join(audit.unknown_handles))}.</p>")
        if audit.unmatched_hosts:
            bits.append(f"<p>Other third-party script hosts: {_e(', '.join(list(audit.unmatched_hosts)[:15]))}.</p>")
        unknown = f'<section><h2>Seen but not yet named</h2><div class="notes">{"".join(bits)}</div></section>'

    notes = [
        "Storefront evidence comes from the public HTML your store serves: app blocks and embeds Shopify marks in the page, "
        "the ScriptTags it injects, and script URLs from known app vendors. Nothing behind a login was read.",
        "Savings count only where a bill shows the charge. Overlap savings assume you keep the most expensive app in the group.",
        "“Paid, no trace” means we did not see the app on the pages we checked. Some apps only load on checkout, account or "
        "specific product pages, so confirm before uninstalling.",
        "Back-office apps such as shipping, accounting and bulk editors never show on the storefront and are never flagged for that.",
    ]

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>App Spend Audit · {_e(host)}</title>
<style>{CSS}</style></head>
<body><main class="page">
<div class="eyebrow">App Spend Audit</div>
<h1>{_e(host)}</h1>
<p class="meta">{_e(audit.generated_at[:10])} · read {_e(_inputs_line(audit))} · catalog {_e(audit.catalog_version)}</p>
<div class="lede">{lede}</div>
<section><h2>Findings</h2><ol class="findings">{findings}</ol></section>
<section><h2>Every app we found</h2>{inventory}</section>
{unknown}
<section><h2>How this was read</h2><div class="notes">{"".join(f"<p>{_e(n)}</p>" for n in notes)}</div>
<span class="caption">Generated locally by App Spend Audit. This file makes no network requests.</span></section>
</main></body></html>
"""
