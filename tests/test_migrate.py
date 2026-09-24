import io
import json
import tempfile
import unittest
from pathlib import Path

from appspend import actions as act
from appspend import playbooks as P
from appspend.fingerprints import Catalog
from appspend.mcp import Server, serve
from appspend.migrate import MigrationError, Runner

from .test_appspend import fake_fetcher

FIX = Path(__file__).parent / "fixtures" / "migrate"
CATALOG = Catalog.load()
SECRET = "sk-test-DO-NOT-STORE"


def env_with(**values):
    return lambda name: values.get(name)


def judgeme_api(counts):
    """A fake Judge.me that returns the next count on each call."""
    seq = iter(counts)

    def fetch(url, headers):
        assert url.startswith("https://api.judge.me/api/v1/reviews/count?")
        return 200, json.dumps({"count": next(seq)})
    return fetch


def ctx(tmp, variant="yotpo", **kw):
    return act.Context(store="linen-and-pine.example", variant=variant, workdir=Path(tmp), **kw)


class PlaybookTests(unittest.TestCase):
    def test_shipped_playbooks_are_valid(self):
        pbs = P.load_all()
        self.assertEqual(set(pbs), {"reviews-to-judgeme", "helpdesk-to-commslayer", "recharge-to-appstle", "loyalty-to-bon"})
        for pb in pbs.values():
            self.assertEqual(P.validate(pb, set(CATALOG.apps), set(act.ACTIONS)), [], pb.id)

    def test_validator_enforces_the_safety_order(self):
        bad = P._parse({
            "id": "x", "title": "x", "category": "x", "to": "X", "summary": "", "checked": "2026-09-24",
            "variants": {"yotpo": {}},
            "steps": [
                {"id": "a", "kind": "merchant_action", "title": "a"},
                {"id": "b", "kind": "merchant_action", "title": "b", "irreversible": True, "cancels_old": True},
                {"id": "c", "kind": "merchant_action", "title": "c", "inputs": [{"name": "k", "type": "secret"}]},
            ],
        })
        errs = " | ".join(P.validate(bad))
        self.assertIn("after a verify step", errs)
        self.assertIn("irreversible steps must be approve steps", errs)
        self.assertIn("secrets go in env vars", errs)
        self.assertIn("first step must be the merchant approving", errs)

    def test_for_app_finds_the_playbook(self):
        self.assertEqual(P.for_app("okendo").id, "reviews-to-judgeme")
        self.assertEqual(P.for_app("smile").id, "loyalty-to-bon")
        self.assertIsNone(P.for_app("klaviyo"))

    def test_fill_leaves_unknown_fields(self):
        self.assertEqual(P.fill("{store} has {r[x][y]} and {nope}", {"store": "s", "r": {}}), "s has {r[x][y]} and {nope}")


class ActionTests(unittest.TestCase):
    def test_reviews_inspect_counts_what_carries_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = act.reviews_inspect_export(ctx(tmp), {"export_file": str(FIX / "yotpo_reviews.csv")})
        d = res["data"]
        self.assertTrue(res["ok"])
        self.assertEqual((d["total"], d["importable"], d["rating_only"], d["bad_rating"]), (6, 4, 1, 1))
        self.assertEqual((d["with_video"], d["photos_dropped"], d["store_reviews"], d["no_metaobject"]), (1, 2, 1, 1))
        self.assertIn("4 of 6 reviews will import", res["summary"])

    def test_reviews_inspect_splits_big_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.csv"
            big.write_text("rating,body\n" + "5,good\n" * (act.JUDGEME_BATCH + 10))
            res = act.reviews_inspect_export(ctx(tmp), {"export_file": str(big)})
            self.assertEqual(len(res["files"]), 2)
            self.assertEqual(res["data"]["batches"], 2)

    def test_judgeme_count_needs_env_and_compares_to_expected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("JUDGEME_API_TOKEN", act.reviews_count_judgeme(ctx(tmp, env=env_with()), {})["summary"])
            env = env_with(JUDGEME_API_TOKEN=SECRET, JUDGEME_SHOP_DOMAIN="x.myshopify.com")
            ok = act.reviews_count_judgeme(ctx(tmp, env=env, fetch=judgeme_api([14]), params={"mode": "verify"},
                                               results={"baseline": {"count": 10}, "inspect": {"importable": 4}}), {})
            self.assertTrue(ok["ok"])
            short = act.reviews_count_judgeme(ctx(tmp, env=env, fetch=judgeme_api([11]), params={"mode": "verify"},
                                                  results={"baseline": {"count": 10}, "inspect": {"importable": 4}}), {})
            self.assertFalse(short["ok"])
            self.assertIn("Don't re-import", short["summary"])

    def test_bon_file_fills_ids_and_flags_tiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = act.loyalty_build_bon_csv(ctx(tmp, "smile"), {
                "export_file": str(FIX / "smile_customers.csv"),
                "shopify_customers_file": str(FIX / "shopify_customers.csv")})
            rows = (Path(tmp) / "bon-points-import.csv").read_text().splitlines()
        d = res["data"]
        self.assertEqual((d["customers"], d["points"], d["zero"], d["missing_id"]), (4, 1016, 1, 1))
        self.assertEqual(rows[0], "Shopify Customer ID,Email,Points")
        self.assertIn("7002,ben@example.com,46", rows)
        self.assertTrue(d["has_tiers"] and d["has_referrals"])
        self.assertIn("VIP tiers", res["summary"])
        self.assertEqual(len(d["spot_check"]), 4)

    def test_recharge_flags_payment_methods_that_cannot_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = act.recharge_inspect_exports(ctx(tmp, "recharge"), {
                "subscriptions_file": str(FIX / "recharge_subscriptions.csv"),
                "payment_methods_file": str(FIX / "recharge_payment_methods.csv")})
        d = res["data"]
        self.assertEqual((d["active"], d["active_customers"], d["at_risk_customers"]), (4, 3, 1))
        self.assertIn("Shopify Support", res["summary"])

    def test_gorgias_inventory_pages_and_lists_active_rules(self):
        pages = {
            "https://acme.gorgias.com/api/rules?limit=100": {"data": [
                {"id": 1, "name": "Auto-close spam", "description": "close it", "code": "Action('setStatus')"},
                {"id": 2, "name": "Old rule", "deactivated_datetime": "2024-01-01"}], "meta": {"next_cursor": "c2"}},
            "https://acme.gorgias.com/api/rules?limit=100&cursor=c2": {"data": [{"id": 3, "name": "Tag VIP"}], "meta": {}},
            "https://acme.gorgias.com/api/macros?limit=100": {"data": [{"id": 9, "name": "Where is my order"}], "meta": {}},
        }
        seen_auth = []

        def fetch(url, headers):
            seen_auth.append(headers.get("Authorization", ""))
            return (200, json.dumps(pages[url])) if url in pages else (404, "")
        env = env_with(GORGIAS_DOMAIN="acme", GORGIAS_EMAIL="ops@acme.test", GORGIAS_API_KEY=SECRET)
        with tempfile.TemporaryDirectory() as tmp:
            res = act.helpdesk_inventory(ctx(tmp, "gorgias", env=env, fetch=fetch), {})
            sheet = (Path(tmp) / "rebuild-sheet.md").read_text()
        self.assertTrue(res["ok"], res["summary"])
        self.assertEqual(res["data"]["counts"]["Rules"], {"total": 3, "active": 2})
        self.assertIn("Auto-close spam", sheet)
        self.assertNotIn("Old rule", sheet)
        self.assertTrue(all(a.startswith("Basic ") for a in seen_auth))
        self.assertNotIn(SECRET, sheet)

    def test_storefront_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = act.storefront_check(ctx(tmp, "yotpo", params={"expect": "judgeme", "_page_fetcher": fake_fetcher}), {})
        self.assertTrue(res["ok"], res["summary"])
        self.assertIn("Judge.me is loading", res["summary"])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = env_with(JUDGEME_API_TOKEN=SECRET, JUDGEME_SHOP_DOMAIN="linen-and-pine.myshopify.com")
        self.r = Runner(root=Path(self.tmp.name), env=self.env, fetch=judgeme_api([10, 14]), page_fetcher=fake_fetcher)
        self.m = self.r.start("https://www.linen-and-pine.example/", "reviews-to-judgeme", "yotpo")

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_is_resumable_and_names_the_switch(self):
        self.assertEqual(self.m.id, "linen-and-pine-example--yotpo-to-judgeme")
        again = self.r.start("linen-and-pine.example", "reviews-to-judgeme", "yotpo")
        self.assertEqual(again.id, self.m.id)
        with self.assertRaises(MigrationError):
            self.r.start("x.example", "reviews-to-judgeme", "gorgias")

    def test_first_step_shows_losses_and_needs_explicit_approval(self):
        st = self.r.status(self.m)
        self.assertEqual(st["next"]["step"], "plan")
        self.assertTrue(any("videos" in x.lower() for x in st["next"]["loses"]))
        with self.assertRaises(MigrationError):
            self.r.complete(self.m, "plan")
        with self.assertRaises(MigrationError):
            self.r.complete(self.m, "install")  # the gate before it isn't done
        self.r.complete(self.m, "plan", approved_by="Sam (owner)")
        self.assertEqual(self.r.load(self.m.id).steps["plan"]["approved_by"], "Sam (owner)")

    def test_full_walkthrough_and_cancel_comes_last(self):
        r, m = self.r, self.m
        r.complete(m, "plan", approved_by="Sam")
        r.complete(m, "install")
        r.complete(m, "keys")
        with self.assertRaises(MigrationError):
            r.complete(m, "baseline")  # automatic steps are run, not ticked
        self.assertTrue(r.run(m, "baseline")["ok"])
        r.complete(m, "export")
        with self.assertRaises(MigrationError):
            r.run(m, "inspect")  # no export file yet
        r.set_input(m, "export_file", str(FIX / "yotpo_reviews.csv"))
        self.assertTrue(r.run(m, "inspect")["ok"])
        hello = r.describe(m, r._step(m, "hello"))
        self.assertIn("about 6 reviews", hello["message"]["body"])
        r.skip(m, "hello", "not needed")
        r.wait(m, "import", "file uploaded")
        with self.assertRaises(MigrationError):
            r.complete(m, "approve_cancel", approved_by="Sam")  # verify hasn't passed
        r.complete(m, "import")
        self.assertTrue(r.run(m, "verify")["ok"])
        r.complete(m, "widgets")
        self.assertTrue(r.run(m, "live_check")["ok"])
        r.complete(m, "approve_cancel", approved_by="Sam")
        r.complete(m, "cancel")
        self.assertEqual(r.current(m).id, "final_check")

    def test_secrets_never_reach_state(self):
        self.r.complete(self.m, "plan", approved_by="Sam")
        self.r.complete(self.m, "install")
        self.r.complete(self.m, "keys")
        self.r.run(self.m, "baseline")
        text = "".join(p.read_text() for p in Path(self.tmp.name).rglob("*") if p.is_file())
        self.assertNotIn(SECRET, text)
        self.assertEqual(self.r.describe(self.m, self.r._step(self.m, "keys"))["env"]["JUDGEME_API_TOKEN"], "set")
        with self.assertRaises(MigrationError):
            self.r.set_input(self.m, "api_token", SECRET)

    def test_variant_env_expands(self):
        m = self.r.start("acme.example", "helpdesk-to-commslayer", "zendesk")
        env = self.r.describe(m, self.r._step(m, "keys"))["env"]
        self.assertEqual(set(env), {"ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN"})

    def test_every_variant_walks_to_the_end_in_order(self):
        """Complete every step of every playbook the lazy way and check nothing is unreachable."""
        for pb in self.r.playbooks.values():
            for variant in pb.variants:
                m = self.r.start(f"{variant}.example", pb.id, variant)
                for s in self.r.steps(m):
                    m.steps[s.id] = {"status": "done"}  # simulate
                self.assertIsNone(self.r.current(m))
                for s in self.r.steps(m):
                    self.assertNotIn("—", self.r.describe(m, s)["instructions"])


class McpTests(unittest.TestCase):
    def test_protocol_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Server(Runner(root=Path(tmp), env=env_with()))
            lines = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "start_migration", "arguments": {
                    "store": "acme.example", "playbook": "loyalty-to-bon", "from_app": "smile"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "complete_step", "arguments": {
                    "migration": "acme-example--smile-to-bon", "step": "plan"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "bogus"},
            ]
            out = io.StringIO()
            serve(io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n"), out, server)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([x["id"] for x in replies], [1, 2, 3, 4, 5])
        self.assertIn("Never ask for API keys", replies[0]["result"]["instructions"])
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertTrue({"audit", "start_migration", "run_step", "complete_step"} <= names)
        status = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual(status["next"]["step"], "plan")
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertIn("explicit yes", replies[3]["result"]["content"][0]["text"])
        self.assertEqual(replies[4]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
