"""A stdio MCP server so any AI harness can run audits and walk a merchant through a migration.

Stdlib only: newline-delimited JSON-RPC 2.0 on stdin/stdout, implementing initialize, tools/list and
tools/call. Register it with a harness as the command `appspend mcp` (or `python -m appspend mcp`).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .migrate import MigrationError, Runner
from .playbooks import for_app

PROTOCOL = "2025-06-18"

INSTRUCTIONS = """appspend finds wasted Shopify app spend and walks a merchant through switching to cheaper tools.

Conventions for driving a migration:
1. Start with the audit. Show savings and, for each switch, what the merchant would lose, before proposing a migration.
2. Walk one step at a time: call migration_status, do what the next step says, then run_step (auto and checks), complete_step, or wait_step.
3. approve steps need the merchant's explicit yes in this conversation. Pass approved_by with their name. Never approve on their behalf.
4. message steps are drafts. Show the draft; the merchant sends it from their own account. Never send it yourself unless they ask you to with a tool they gave you.
5. Never ask for API keys or tokens in chat. Tell the merchant which environment variables to set in their own shell; migration_status shows whether they're set.
6. agent steps are yours: do them with the tools the harness has (for example a vendor's MCP), as drafts the merchant reviews.
7. The old tool is cancelled last, only after the checks pass. If a check fails, stop and explain; don't skip ahead.
"""


def _schema(**props: tuple[str, bool]) -> dict:
    """An input schema from name=(description, optional) pairs; every field is a string."""
    return {"type": "object",
            "properties": {k: {"type": "string", "description": d} for k, (d, _) in props.items()},
            "required": [k for k, (_, opt) in props.items() if not opt]}


def _field(description: str, optional: bool = False) -> tuple[str, bool]:
    return description, optional


def _tools() -> list[dict]:
    return [
        {"name": "audit", "description": "Audit a Shopify store's apps from its public storefront, optionally with a bills CSV. Returns apps found, findings, cheaper paths and which switches have a guided migration.",
         "inputSchema": _schema(store=_field("store domain, e.g. example.com"), bills=_field("path to a Shopify bills export CSV", True))},
        {"name": "list_playbooks", "description": "List the guided migrations appspend can run and which apps each moves a store off.",
         "inputSchema": _schema()},
        {"name": "list_migrations", "description": "List migrations in progress on this machine.", "inputSchema": _schema()},
        {"name": "start_migration", "description": "Start (or resume) a migration for a store. from_app is the catalog id of the tool being left, e.g. yotpo, gorgias, recharge, smile.",
         "inputSchema": _schema(store=_field("store domain"), playbook=_field("playbook id, e.g. reviews-to-judgeme"), from_app=_field("catalog id of the current app"))},
        {"name": "migration_status", "description": "All steps with their status, plus full details of the next step: instructions, inputs, env vars (set or missing, never values), message drafts and what the merchant loses.",
         "inputSchema": _schema(migration=_field("migration id"))},
        {"name": "set_input", "description": "Record a non-secret input for a migration, such as the path to an export file.",
         "inputSchema": _schema(migration=_field("migration id"), name=_field("input name"), value=_field("value, e.g. a file path"))},
        {"name": "run_step", "description": "Run an automatic step or check (kinds auto and verify with an action). Returns ok, a summary for the merchant, data and files written.",
         "inputSchema": _schema(migration=_field("migration id"), step=_field("step id"))},
        {"name": "complete_step", "description": "Mark a merchant, agent, vendor, message or manual check step done. approve steps require approved_by: the merchant's explicit yes in this conversation.",
         "inputSchema": _schema(migration=_field("migration id"), step=_field("step id"), note=_field("what happened", True), approved_by=_field("who approved (approve steps only)", True))},
        {"name": "wait_step", "description": "Mark a step as waiting on someone (usually the vendor's team) so the migration can pause and resume later.",
         "inputSchema": _schema(migration=_field("migration id"), step=_field("step id"), note=_field("what we're waiting for", True))},
        {"name": "skip_step", "description": "Skip an optional step.",
         "inputSchema": _schema(migration=_field("migration id"), step=_field("step id"), reason=_field("why"))},
    ]


class Server:
    def __init__(self, runner: Runner | None = None):
        self.runner = runner or Runner()

    def _audit(self, store: str, bills: str | None = None) -> dict:
        from .alternatives import load as load_paths
        from .analyze import analyze
        from .bills import parse_bills
        from .fingerprints import Catalog
        from .scan import scan_storefront

        catalog = Catalog.load()
        scan = scan_storefront(store, catalog, pages=3)
        audit = analyze(catalog, scan, None, parse_bills(bills, catalog) if bills else None,
                        pages_checked=len(scan.pages), paths=load_paths())
        data = audit.to_dict()
        guided = {}
        for it in audit.inventory:
            if it.app and (pb := for_app(it.app.id, self.runner.playbooks)):
                guided[it.app.id] = pb.id
        data["guided_migrations"] = guided
        return data

    def call(self, name: str, a: dict) -> Any:
        r = self.runner
        if name == "audit":
            return self._audit(a["store"], a.get("bills"))
        if name == "list_playbooks":
            return [{"id": p.id, "title": p.title, "to": p.to, "from_apps": {k: v.get("name", k) for k, v in p.variants.items()},
                     "summary": p.summary, "checked": p.checked} for p in r.playbooks.values()]
        if name == "list_migrations":
            return [{"migration": m.id, "store": m.store, "playbook": m.playbook,
                     "next": (nxt.id if (nxt := r.current(m)) else None)} for m in r.all()]
        if name == "start_migration":
            return r.status(r.start(a["store"], a["playbook"], a["from_app"]))
        m = r.load(a["migration"])
        if name == "migration_status":
            return r.status(m)
        if name == "set_input":
            r.set_input(m, a["name"], a["value"])
            return r.status(m)
        if name == "run_step":
            return {"result": r.run(m, a["step"]), "status": r.status(m)}
        if name == "complete_step":
            r.complete(m, a["step"], a.get("note", ""), a.get("approved_by", ""))
            return r.status(m)
        if name == "wait_step":
            r.wait(m, a["step"], a.get("note", ""))
            return r.status(m)
        if name == "skip_step":
            r.skip(m, a["step"], a["reason"])
            return r.status(m)
        raise MigrationError(f"unknown tool {name}")

    def handle(self, msg: dict) -> dict | None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                result: Any = {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                               "capabilities": {"tools": {}},
                               "serverInfo": {"name": "appspend", "version": __version__},
                               "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": _tools()}
            elif method == "tools/call":
                try:
                    out = self.call(params["name"], params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": json.dumps(out, indent=1, default=str)}]}
                except (MigrationError, ValueError, KeyError, FileNotFoundError) as e:
                    result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:  # keep the server alive for the harness
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def serve(stdin=sys.stdin, stdout=sys.stdout, server: Server | None = None) -> None:
    server = server or Server()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply: dict | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
