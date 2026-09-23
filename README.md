# App Spend Audit

Find the Shopify apps a store pays for and doesn't use.

App Spend Audit reads three things you already have: the public storefront, the theme file, and the bills export. It lines them up and tells you which apps are billed but not running, which ones do the same job twice, and what old apps left behind in the code. Each finding comes with a dollar figure, a confidence level, and the evidence behind it.

It runs on your machine and needs nothing beyond Python 3.10. No accounts, no API keys, no store login.

```
$ appspend audit linen-and-pine.example --theme theme.zip --bills bills.csv

appspend · linen-and-pine.example
read: 2 storefront pages, theme (9 files), bills (11 apps, through 2026-08-01)

App spend      $680/mo
Likely savings $83/mo  ($996/yr)
Worth checking $429/mo more

Findings
 1. Hotjar is billed but not running on your store  $39/mo  [High confidence]
 2. 2 chat and helpdesk apps doing one job  $29/mo  [Likely]
 3. 2 product reviews apps doing one job  $15/mo  [Likely]
 4. Swym Wishlist Plus code is live but not on your bills  [Likely]
 5. 3 app snippets left behind in the theme  [Likely]
 ...
```

Open [`examples/demo-report.html`](examples/demo-report.html) to see the full report for the demo store.

## Install

```
pipx install git+https://github.com/mindmelding/appspend
```

Or run it from a checkout with `python3 -m appspend`.

## Use

| Command | What it does |
|---|---|
| `appspend audit STORE [--theme T] [--bills B]` | Full audit. Writes an HTML report and records the run. |
| `appspend scan STORE` | Quick list of the apps a storefront loads. |
| `appspend history STORE` | Past audits of a store, so you can see what changed. |
| `appspend catalog [QUERY]` | The apps appspend can recognize. |

Audit flags: `--out report.html`, `--json audit.json`, `--md summary.md`, `--pages N` (default 3), `--no-history`, `--db PATH`, and `--catalog custom.json` for your own fingerprints.

Any input works on its own. A storefront scan alone lists apps and overlaps. Add bills to get dollar figures. Add the theme to raise confidence and find leftover code.

### Getting the inputs

- **Storefront:** just the domain. appspend reads the home page, one product page and one collection page.
- **Theme:** Shopify admin, Online Store, Themes, the `...` menu on the live theme, Download theme file. Shopify emails a zip. Pass the zip or the unzipped folder.
- **Bills:** Settings, Billing, Export bills. Shopify emails a CSV. Shopify doesn't publish the column layout, so appspend finds the columns by name. A plain two-column sheet works too:

  ```
  app,monthly_cost
  Judge.me,15
  Hotjar,39
  ```

## How it decides

**What it reads on the storefront.** Shopify marks app blocks and app embeds in the page, and it injects older apps through a `ScriptTag` loader. appspend reads those markers first, then checks script, stylesheet, iframe and image URLs against a catalog of 126 apps in 34 categories. It ignores links in navigation text, so a "Read our Trustpilot reviews" link doesn't count as Trustpilot running.

**Findings, from most to least certain:**

| Finding | Meaning | Counted as savings |
|---|---|---|
| Billed, not running | A charge on the latest bill, and no trace on the storefront or in the theme | Yes. High confidence with all three inputs, lower with fewer |
| Doing one job twice | Two paid apps in a category where one is normal, like two review apps | Yes, assuming you keep the more expensive one |
| Code with no bill | App code is live but nothing on the bill. Free plan, or leftovers | No. It slows pages |
| Theme leftovers | Snippets nothing renders any more, and switched-off embeds | No |
| Price creep, usage spikes | Recurring price up 15% or more, or usage fees at 1.5x their usual level | No, for your records |
| Free option | Shopify or a vendor offers the core job free | Shown apart as "worth checking" |

**What it won't flag.** Back-office apps (shipping, accounting, bulk editing) and apps that often run without storefront code (helpdesks, email, fraud screening) are never called "not running" just because the storefront is quiet. From a theme alone, "not running" stays a lead to check, because many apps load without touching theme files.

**Limits worth knowing.** Some apps only load on checkout, account or specific product pages. appspend checks three pages by default, so confirm before you uninstall. Snippets rendered by a variable name look orphaned to static analysis. The bills parser was built against Shopify's export as best documented, and it tells you which columns it picked (`--json` shows them) so a wrong guess is easy to spot.

## Privacy and manners

- It reads public pages only, one request per page, with no retries and no way around blocks. The user agent names the tool and nothing about you.
- Reports are single HTML files with no scripts and no outside requests. You can email one to a client as is.
- Run history lives in `~/.appspend/history.sqlite` on your machine.

## Development

```
python3 -m unittest discover -s tests -t .
python3 examples/build_demo.py   # rebuild the sample report
```

The fingerprint catalog is `appspend/data/apps.json`, one app per line. An entry needs an `id`, `name`, `category`, and `urls` (substrings of script URLs) and/or `keys` (matched against app handles, snippet names and bill lines, lowercase letters and digits only). Mark `"storefront": false` for back-office apps and `"optional": true` for apps that often run with no storefront code. The longest matching pattern wins, so `cdn-loyalty.yotpo.com` beats `yotpo.com`.

When a scan reports an unrecognized app handle or script host, that's the next catalog entry to add.
