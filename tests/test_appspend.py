import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from appspend import history
from appspend.analyze import analyze
from appspend.bills import parse_amount, parse_bills, parse_date
from appspend.cli import main
from appspend.fingerprints import Catalog
from appspend.report import render_html, render_markdown, render_text
from appspend.scan import Page, normalize_store, scan_storefront
from appspend.theme import scan_theme

FIX = Path(__file__).parent / "fixtures"
CATALOG = Catalog.load()


def fake_fetcher(url: str) -> Page:
    pages = {
        "https://linen-and-pine.example/": "storefront_home.html",
        "https://linen-and-pine.example/products/linen-duvet": "storefront_product.html",
    }
    name = pages.get(url)
    if not name:
        return Page(url, 404, "", error="HTTP 404")
    return Page(url, 200, (FIX / name).read_text())


def full_audit(paths=None):
    """paths={} keeps the tests independent of the shipped research data; pass None to use it."""
    scan = scan_storefront("linen-and-pine.example", CATALOG, fetcher=fake_fetcher)
    theme = scan_theme(FIX / "theme", CATALOG)
    bills = parse_bills(FIX / "bills_shopify.csv", CATALOG)
    return analyze(CATALOG, scan, theme, bills, paths={} if paths is None else paths)


class CatalogTests(unittest.TestCase):
    def test_catalog_is_consistent(self):
        self.assertGreater(len(CATALOG.apps), 100)
        for app in CATALOG.apps.values():
            self.assertIn(app.category, CATALOG.categories)
            self.assertTrue(app.urls or app.keys, app.id)

    def test_longest_url_pattern_wins(self):
        self.assertEqual(CATALOG.match_url("https://cdn-loyalty.yotpo.com/loader/x.js").id, "yotpo_loyalty")
        self.assertEqual(CATALOG.match_url("https://staticw2.yotpo.com/abc/widget.js").id, "yotpo")

    def test_key_matching_on_bill_names_and_handles(self):
        self.assertEqual(CATALOG.match_key("Judge.me Product Reviews - Awesome plan").id, "judgeme")
        self.assertEqual(CATALOG.match_key("klaviyo-email-marketing").id, "klaviyo")
        self.assertEqual(CATALOG.match_key("swym-relay").id, "swym")
        self.assertIsNone(CATALOG.match_key("order-limits-magic"))
        self.assertEqual(CATALOG.match_key("inbox").id, "shopify_inbox")
        self.assertIsNone(CATALOG.match_key("typeforms-embed"))  # generic words only match exactly

    def test_known_false_overlaps_stay_fixed(self):
        # Consentmo ships an embed called gdpr-backpack: one app, not two
        self.assertEqual(CATALOG.match_key("gdpr-backpack").id, "consentmo")
        # Yotpo's unified loader serves reviews, loyalty and subscriptions: not proof of reviews
        self.assertEqual(CATALOG.match_url("https://cdn-widgetsrepository.yotpo.com/v1/loader/x").id, "yotpo_platform")
        self.assertEqual(CATALOG.match_url("https://staticw2.yotpo.com/abc/widget.js").id, "yotpo")
        # server-side tagging and attribution dashboards are different jobs
        self.assertFalse(CATALOG.categories[CATALOG.apps["elevar"].category].exclusive)

    def test_no_url_pattern_matches_platform_hosts(self):
        for host in ("https://cdn.shopify.com/s/files/1/theme.js", "https://shop.app/pay", "https://www.google.com/x"):
            self.assertIsNone(CATALOG.match_url(host), host)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.scan = scan_storefront("linen-and-pine.example", CATALOG, fetcher=fake_fetcher)

    def test_normalize_store(self):
        self.assertEqual(normalize_store("Example.com/some/path"), "https://example.com")
        self.assertEqual(normalize_store("http://x.myshopify.com"), "https://x.myshopify.com")

    def test_reads_home_and_one_product_page(self):
        urls = [p["url"] for p in self.scan.pages]
        self.assertIn("https://linen-and-pine.example/products/linen-duvet", urls)
        self.assertTrue(self.scan.is_shopify)

    def test_detects_every_signal_type(self):
        d = self.scan.detections
        self.assertEqual({e.source for e in d["judgeme"]} >= {"scripttag"}, True)
        self.assertIn("app-block", {e.source for e in d["loox"]})
        self.assertIn("app-embed", {e.source for e in d["rebuy"]})
        self.assertIn("app-embed", {e.source for e in d["swym"]})
        for app_id in ("privy", "klaviyo", "tidio"):
            self.assertIn(app_id, d)

    def test_navigation_links_are_not_evidence(self):
        self.assertNotIn("trustpilot", self.scan.detections)

    def test_unknown_handles_and_hosts_are_kept(self):
        self.assertIn("order-limits-magic", self.scan.unknown_handles)
        self.assertIn("cdn.unknownvendor.io", self.scan.third_party_hosts)

    def test_store_own_assets_never_match_a_vendor(self):
        html = '<script>Shopify.shop="x"</script><link rel="preload" href="https://www.bradleymountain.com/cdn/shop/t/1/assets/a.js">'
        r = scan_storefront("bradleymountain.com", CATALOG,
                            fetcher=lambda u: Page(u, 200, html) if u.endswith(".com/") else Page(u, 404, ""))
        self.assertEqual(r.detections, {})

    def test_unreachable_store(self):
        r = scan_storefront("nowhere.example", CATALOG, fetcher=lambda u: Page(u, 0, "", error="timeout"))
        self.assertEqual(r.detections, {})
        self.assertEqual(r.pages[0]["error"], "timeout")


class ThemeTests(unittest.TestCase):
    def test_theme_folder(self):
        t = scan_theme(FIX / "theme", CATALOG)
        self.assertIn("judgeme", t.detections)
        self.assertIn("tidio", t.detections)
        self.assertIn("loox", t.detections)  # app block in templates/product.json
        orphans = {o["name"]: o["app_id"] for o in t.orphan_snippets}
        self.assertEqual(orphans.get("snippets/pagefly-app-header.liquid"), "pagefly")
        self.assertEqual(orphans.get("snippets/wishlisthero-header.liquid"), "wishlisthero")
        self.assertEqual(orphans.get("snippets/old-popup.liquid"), "sumo")  # attributed by URL inside
        self.assertNotIn("snippets/social-meta.liquid", orphans)
        self.assertNotIn("pagefly", t.detections)  # orphaned code is not live evidence
        disabled = [e for e in t.app_embeds if e["disabled"]]
        self.assertEqual([e["app_id"] for e in disabled], ["wisepops"])

    def test_theme_zip_with_wrapper_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            z = Path(tmp) / "theme.zip"
            with zipfile.ZipFile(z, "w") as zf:
                for f in (FIX / "theme").rglob("*"):
                    if f.is_file():
                        zf.write(f, "dawn-export/" + f.relative_to(FIX / "theme").as_posix())
            t = scan_theme(z, CATALOG)
            self.assertIn("judgeme", t.detections)
            self.assertEqual(len(t.orphan_snippets), 3)

    def test_rejects_non_theme(self):
        with self.assertRaises(ValueError):
            scan_theme(FIX / "bills_manual.csv", CATALOG)


class BillsTests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(parse_amount("$1,234.50"), 1234.5)
        self.assertEqual(parse_amount("€150,00"), 150.0)
        self.assertEqual(parse_amount("1.234,56"), 1234.56)
        self.assertEqual(parse_amount("(20.00)"), -20.0)
        self.assertIsNone(parse_amount("n/a"))

    def test_dates(self):
        self.assertEqual(str(parse_date("2026-08-01 00:00:00 -0700")), "2026-08-01")
        self.assertEqual(str(parse_date("Aug 1, 2026")), "2026-08-01")

    def test_shopify_export(self):
        b = parse_bills(FIX / "bills_shopify.csv", CATALOG)
        self.assertEqual(b.format, "shopify-export")
        self.assertEqual(str(b.as_of), "2026-08-01")
        self.assertNotIn("shopifyplan", b.apps)                  # plan fee excluded
        self.assertFalse(any("transaction" in k for k in b.apps))
        self.assertEqual(b.apps["klaviyo"].monthly, 150.0)
        self.assertEqual(b.apps["rebuy"].recurring_series()[0][1], 99.0)
        gorgias = b.apps["gorgias"]
        self.assertAlmostEqual(gorgias.monthly, 60 + (22 + 21 + 71.5) / 3, places=2)
        self.assertTrue(b.active(b.apps["hotjar"]))
        self.assertFalse(b.active(b.apps["pagefly"]))            # last billed in July, dropped in August
        self.assertIn("orderlimitsmagic", b.apps)

    def test_manual_sheet(self):
        b = parse_bills(FIX / "bills_manual.csv", CATALOG)
        self.assertEqual(b.format, "manual")
        self.assertEqual(b.apps["loox"].monthly, 29.99)
        self.assertIn("somecustomapp", b.apps)

    def test_semicolon_european(self):
        b = parse_bills(FIX / "bills_semicolon.csv", CATALOG)
        self.assertEqual(list(b.apps), ["klaviyo"])
        self.assertEqual(b.apps["klaviyo"].monthly, 150.0)

    def test_unreadable_columns(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("foo,bar\n1,2\n")
        with self.assertRaises(ValueError):
            parse_bills(f.name, CATALOG)


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.audit = full_audit()
        self.by_kind = {}
        for f in self.audit.findings:
            self.by_kind.setdefault(f.kind, []).append(f)

    def test_paid_but_not_running(self):
        titles = [f.title for f in self.by_kind["paid_no_trace"]]
        self.assertTrue(any("Hotjar" in t for t in titles))
        self.assertFalse(any("ShipStation" in t for t in titles))       # back office, expected invisible
        self.assertFalse(any("Order Limits" in t for t in titles))      # tied to its unrecognized app block
        hotjar = next(f for f in self.by_kind["paid_no_trace"] if "Hotjar" in f.title)
        self.assertEqual(hotjar.confidence, "high")
        self.assertEqual(hotjar.monthly_savings, 39.0)

    def test_overlap_keeps_most_expensive(self):
        reviews = next(f for f in self.by_kind["overlap"] if "Judge.me" in f.apps)
        self.assertEqual(set(reviews.apps), {"Judge.me", "Loox"})
        self.assertEqual(reviews.monthly_savings, 15.0)

    def test_leftovers(self):
        self.assertIn("theme_leftovers", self.by_kind)
        self.assertIn("disabled_embeds", self.by_kind)
        leftover = {a for f in self.by_kind.get("leftover_code", []) for a in f.apps}
        self.assertIn("Swym Wishlist Plus", leftover)

    def test_history_findings(self):
        self.assertTrue(any("Rebuy" in f.title for f in self.by_kind["price_creep"]))
        self.assertTrue(any("Gorgias" in f.title for f in self.by_kind["usage_spike"]))

    def test_free_option_is_low_confidence_and_separate(self):
        free = self.by_kind["free_option"]
        self.assertTrue(all(f.confidence == "low" for f in free))
        self.assertFalse(any("Hotjar" in f.apps for f in free))  # already flagged as unused

    def test_totals(self):
        a = self.audit
        self.assertEqual(a.savings("high", "medium"), 39.0 + 15.0 + 29.0)  # Hotjar unused, Judge.me and Tidio overlaps
        self.assertAlmostEqual(a.total_monthly_spend, 680.14, places=2)  # August run rate, usage averaged
        self.assertEqual(a.findings[0].confidence, "high")

    def test_inline_mentions_alone_do_not_make_an_overlap(self):
        from appspend.scan import Evidence
        scan = scan_storefront("linen-and-pine.example", CATALOG, fetcher=fake_fetcher)
        scan.detections["attentive"] = [Evidence("inline", "creatives.attn.tv", "/")]
        scan.detections["postscript"] = [Evidence("scripttag", "sdk.postscript.io/sdk.js", "/")]
        a = analyze(CATALOG, scan, paths={})
        self.assertFalse(any(f.kind == "overlap" and "Attentive" in f.apps for f in a.findings))
        scan.detections["attentive"].append(Evidence("scripttag", "cdn.attn.tv/x/dtag.js", "/"))
        a = analyze(CATALOG, scan, paths={})
        self.assertTrue(any(f.kind == "overlap" and "Attentive" in f.apps for f in a.findings))
        # inline code that loads a real script file does count (accessiBe, LoyaltyLion load this way)
        scan.detections["attentive"] = [Evidence("inline", "cdn.attn.tv/brand/dtag.js", "/")]
        a = analyze(CATALOG, scan, paths={})
        self.assertTrue(any(f.kind == "overlap" and "Attentive" in f.apps for f in a.findings))

    def test_scan_only_mode_has_no_money(self):
        scan = scan_storefront("linen-and-pine.example", CATALOG, fetcher=fake_fetcher)
        a = analyze(CATALOG, scan)
        self.assertEqual(a.savings("high", "medium", "low"), 0)
        self.assertTrue(any(f.kind == "overlap" for f in a.findings))  # Judge.me + Loox still visible


class ShippedDataTests(unittest.TestCase):
    def test_shipped_alternatives_are_valid_and_sourced(self):
        from appspend import alternatives
        paths = alternatives.load()
        self.assertGreaterEqual(len(paths), 30)
        for app_id, p in paths.items():
            self.assertIn(app_id, CATALOG.apps)
            self.assertTrue(p.incumbent_source.startswith("https://"), app_id)
            for a in p.alternatives:
                self.assertTrue(a.source.startswith("https://"), f"{app_id}: {a.name}")
                self.assertTrue(a.checked, f"{app_id}: {a.name}")


class CheaperPathTests(unittest.TestCase):
    def setUp(self):
        from appspend import alternatives
        self.paths = alternatives.load(FIX / "alternatives.json")
        scan = scan_storefront("linen-and-pine.example", CATALOG, fetcher=fake_fetcher)
        self.bills = parse_bills(FIX / "bills_shopify.csv", CATALOG)
        self.audit = analyze(CATALOG, scan, scan_theme(FIX / "theme", CATALOG), self.bills, paths=self.paths)
        self.scan_only = analyze(CATALOG, scan, paths=self.paths)

    def test_replace_path_with_bills_counts_the_gap(self):
        f = next(f for f in self.audit.findings if f.kind == "cheaper_path" and "Privy" in f.apps)
        self.assertIn("Shopify Forms (free)", f.title)
        self.assertEqual(f.monthly_savings, 30.0)
        self.assertEqual(f.confidence, "low")
        self.assertIn("Spin-to-win", f.misses)
        self.assertTrue(any("privy.com" in s for s in f.sources))

    def test_negotiate_path_has_no_savings_and_flags_stale_prices(self):
        f = next(f for f in self.audit.findings if f.kind == "cheaper_path" and "Klaviyo" in f.apps)
        self.assertEqual(f.monthly_savings, 0)
        self.assertEqual(f.confidence, "info")
        self.assertIn("recheck", f.detail)

    def test_apps_already_flagged_unused_get_no_path(self):
        self.assertFalse(any(f.kind == "cheaper_path" and "Hotjar" in f.apps for f in self.audit.findings))

    def test_scan_only_skips_plan_and_contract_guesses(self):
        self.assertFalse(any(f.kind == "cheaper_path" and "Klaviyo" in f.apps for f in self.scan_only.findings))

    def test_scan_only_shows_list_price(self):
        f = next(f for f in self.scan_only.findings if f.kind == "cheaper_path" and "Privy" in f.apps)
        self.assertIn("lists at $30–$499/mo", f.detail)
        self.assertEqual(f.monthly_savings, 0)

    def test_bad_entry_rejected(self):
        from appspend import alternatives
        with self.assertRaises(ValueError):
            alternatives._load_entries({"entries": [{"app": "x", "switch_cost": "trivial", "strategy": "replace"}]})

    def test_html_renders_fit(self):
        html = render_html(self.audit)
        self.assertIn("Easy switch", html)
        self.assertIn("Spin-to-win", html)


class OutputTests(unittest.TestCase):
    def test_renderers(self):
        a = full_audit()
        html = render_html(a)
        self.assertIn("linen-and-pine.example", html)
        self.assertNotIn("<script", html)                # report is inert
        self.assertNotRegex(html, r"https?://(?!linen)")  # no external requests
        self.assertIn("Hotjar", render_markdown(a))
        self.assertIn("Likely savings", render_text(a, color=False))
        json.dumps(a.to_dict())

    def test_html_escapes_untrusted_names(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write('app,monthly_cost\n"<img src=x onerror=alert(1)>",10\n')
        a = analyze(CATALOG, bills=parse_bills(f.name, CATALOG))
        self.assertNotIn("<img src=x", render_html(a))

    def test_history_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "h.sqlite"
            a = full_audit().to_dict()
            with history.connect(db) as conn:
                self.assertIsNone(history.save(conn, a))
                b = json.loads(json.dumps(a))
                b["inventory"] = [i for i in b["inventory"] if i["name"] != "Hotjar"]
                b["generated_at"] = "2099-01-01T00:00:00Z"
                diff = history.save(conn, b)
                self.assertEqual(diff["removed"], ["Hotjar"])
                self.assertEqual(len(history.runs(conn, a["store"])), 2)

    def test_cli_audit_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.html"
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = main(["audit", "--theme", str(FIX / "theme"), "--bills", str(FIX / "bills_shopify.csv"),
                             "--out", str(out), "--json", str(Path(tmp) / "r.json"), "--no-history"])
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())
            data = json.loads((Path(tmp) / "r.json").read_text())
            self.assertGreater(data["summary"]["savings_monthly_confirmed"], 0)

    def test_batch_ranks_overlaps_first(self):
        from appspend.batch import read_domains, run_batch, write_csv
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "d.csv"
            src.write_text("domain,name,city\nlinen-and-pine.example,Linen & Pine,Oceanside\nnowhere.example,Gone,Vista\nlinen-and-pine.example,dupe,x\n")
            rows = read_domains(src)
            self.assertEqual(len(rows), 2)  # duplicate dropped
            ranked = run_batch(rows, CATALOG, delay=0, fetcher=fake_fetcher)
            top = ranked[0]
            self.assertEqual(top.domain, "linen-and-pine.example")
            self.assertTrue(any("Judge.me" in o and "Loox" in o for o in top.overlaps))
            self.assertIn("Most stores keep one", top.pitch)
            self.assertTrue(ranked[1].status.startswith("unreachable"))
            out = Path(tmp) / "r.csv"
            write_csv(ranked, out)
            self.assertIn("Oceanside", out.read_text())  # extra columns carried through

    def test_cli_needs_input(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["audit"]), 2)


if __name__ == "__main__":
    unittest.main()
