# Migration paths into the cheaper tools (checked 2026-09-24)

Question: for each switch Swap My Stack recommends, can a "Yes, migrate" button kick it off?
Each path was checked against vendor help docs, API docs and GitHub. No open-source migration script exists for any pair.

## Tier 1: we can run it (public APIs, merchant grants access)

| Switch | How | Lost |
|---|---|---|
| Rise.ai → Shopify store credit | Rise export CSV → `storeCreditAccountCredit` per customer. Gift cards already live in Shopify | Claim page, greeting cards |
| Yotpo, Okendo, Stamped, Loox → Judge.me | Source CSV export → Judge.me import wizard (built-in importer for all four), or `POST /api/v1/reviews` | Videos. Okendo: verified badges and Google Shopping feed. API-created reviews can't be verified, so prefer the importer |
| Bazaarvoice → Judge.me | Conversations API read → Judge.me API or generic CSV wizard | Verified status, videos, syndication |
| Mailchimp, Klaviyo contacts → Judge.me Email | Source API → Shopify `customerEmailMarketingConsentUpdate`. Judge.me Email reads Shopify customers directly | Flows (6 fixed ones, toggled on by API), templates, send history, suppression reasons |
| Privy contacts → Shopify Forms | Privy API → Shopify customers with consent | Popups must be rebuilt by hand |
| Hotjar → Microsoft Clarity | Install Clarity app, remove Hotjar | Old recordings and surveys |

## Tier 2: the new vendor does it free (button = send the request with the export attached)

| Switch | How | Lost |
|---|---|---|
| Gorgias → Commslayer | Self-serve importer with a Gorgias API key, about an hour | Automations and rules; imports are read-only |
| Zendesk → Commslayer | Same, with Zendesk API token | Same, macros not listed |
| Kustomer → Commslayer | Listed on the migration page, no help article. Unconfirmed | Unknown |
| Recharge → Appstle | Free on all plans. Recharge Export Builder gives subscriptions + payment tokens; Appstle loads them, 68% done within a day | Apple Pay, Google Pay, PayPal agreements, expired cards |
| Recharge → Shopify Subscriptions | Reshape Recharge export into Shopify's 27-column CSV, merchant uploads | Contracts not on Shopify Checkout, mixed bundles |
| Yotpo Loyalty, LoyaltyLion, Smile → BON | Send points CSV (customer ID, email, points); BON staff load it free | VIP tiers and referral links not in the CSV; unconfirmed whether BON rebuilds them |
| Attentive → Postscript | Attentive rep must send the subscriber export (no self-serve). Postscript compliance uploads in 1 to 3 days | Keywords, flows, the phone number |
| Triple Whale, Intelligems downgrades | Billing change in the app | Paid features |

## Tier 3: rebuild by hand (button = checklist, maybe a theme PR)

| Switch | Why manual |
|---|---|
| Rebuy → FBT + Upcart | Rebuy config is readable by API, but neither destination has a write API |
| Elevar → Google, Meta apps + custom pixel | Elevar exports GTM JSON; custom pixels can only be pasted in the admin |
| accessiBe → theme fixes | axe-core / pa11y scan is scriptable, fixes pushable by Shopify CLI, but the fixes are hand-written |
| PageFly → OS 2.0 sections | No converter. Uninstalling PageFly deletes everything, so rebuild first |
| Swym → Wishlist by Square | No import; shopper wishlists are lost |
| Loop → Shopify returns or Redo | No return-rules API |
| NoFraud → Shopify Protect | Fraud Protect is closed to new stores; Protect covers Shop Pay orders only |
| Consent apps → Shopify banner | A few admin settings; no API |

Sources: see the agent reports summarized in this session; key docs are linked from each vendor's help center (Judge.me migration collection 19736844, docs.commslayer.com migration category, appstle.com/migrations/subscriptions, bonloyalty.com/docs/customers/import-customer-data-via-csv-file, help.postscript.io 13563832, shopify.dev storeCreditAccountCredit and customerEmailMarketingConsentUpdate).
