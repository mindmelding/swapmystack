# Migration playbooks

A playbook moves a store off one app and onto a cheaper one. It is a JSON file in `appspend/data/playbooks/`, so adding a switch means writing data, not code. Any AI assistant runs the same file through `appspend mcp`, and a person can run it with `appspend migrate`.

## Shipped

| Playbook | Moves off | Who does the heavy lifting |
|---|---|---|
| `reviews-to-judgeme` | Yotpo Reviews, Okendo, Stamped, Loox | Judge.me's built-in importer; appspend checks the file first and counts the result after |
| `helpdesk-to-commslayer` | Gorgias, Zendesk | Commslayer's free importer; appspend lists the rules to rebuild, and the assistant rebuilds them through Commslayer's MCP |
| `recharge-to-appstle` | Recharge | Appstle's team, free on every plan; appspend counts subscribers whose payment method can't move |
| `loyalty-to-bon` | Yotpo Loyalty, LoyaltyLion, Smile.io | BON's team loads points by hand; appspend builds their file and picks balances to spot-check |

## Step kinds

| Kind | Who acts | How it completes |
|---|---|---|
| `approve` | the merchant says yes | `done --approved-by NAME`, only after an explicit yes |
| `message` | a draft to a vendor | the merchant reviews it and sends it from their own account |
| `merchant_action` | the merchant, in an admin we can't reach | `done` |
| `agent` | the AI assistant, with tools the harness has | `done` after the merchant reviews the drafts |
| `auto` | appspend | `run` |
| `vendor` | the new vendor's team | `wait`, then `done` |
| `verify` | a check | `run` if it has an action, otherwise `done` after the check |

## Rules every playbook follows

The validator (`playbooks.validate`) and the runner enforce these, and the tests run every shipped playbook through both.

1. **Plan first.** The first step is an `approve` that shows what the merchant loses in the switch.
2. **Gates hold.** Nothing after an `approve` or `verify` step can be completed until that step is done.
3. **Cancel last.** The step that cancels the old tool (`cancels_old`) comes after a `verify` step and an irreversible `approve`, and needs every earlier step done or skipped.
4. **Irreversible means approved.** Only `approve` steps can be marked `irreversible`.
5. **Keys stay in the shell.** Steps name environment variables; the runner reports them as set or missing and never stores a value. Inputs named like keys or tokens are refused.
6. **Talk to the new vendor, not the old one.** Messages to the tool being left are optional, merchant-chosen, and only ever ask for a better price.
7. **Payment data stays with the vendors.** appspend reads counts from exports. It never moves card tokens, SMS consent records or balances itself.
8. **Every claim has a source.** `sources` lists the vendor docs each step came from, and `checked` dates them.
9. **Plain copy.** Step text goes to merchants: short sentences, no jargon, no em dashes.

## Writing a new playbook

- `variants` holds one entry per app the playbook moves off, keyed by its catalog id. Variant text (export steps, key setup) is referenced as `{v[name]}`.
- Earlier results are available as `{r[step_id][field]}`, for example `{r[inspect][importable]}`. Unknown fields stay visible rather than failing.
- `env: ["$variant"]` pulls in the variant's own list of environment variables.
- Add an action to `actions.py` only when a step reads data or checks a result. Actions never write to a vendor or the live store.
- Add a fixture and a test for every new action.
