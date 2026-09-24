"""Run a migration playbook step by step, with state saved locally so it can pause for days.

State lives in ~/.swapstack/migrations/<id>.json next to a work folder for exports and drafts.
The rules that keep it safe are enforced here, not left to whoever drives it:
- an approve step completes only with explicit approval from the merchant
- nothing after a gate (approve or verify) completes until the gate is done
- the step that cancels the old tool needs every earlier step done or skipped
- keys come from environment variables and are never written to state
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import actions as act
from .playbooks import Playbook, Step, fill, load_all
from .scan import normalize_store

DEFAULT_DIR = Path(os.environ.get("SWAPSTACK_HOME", Path.home() / ".swapstack")) / "migrations"
DONE = {"done", "skipped"}


class MigrationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Migration:
    id: str
    playbook: str
    variant: str
    store: str
    created: str
    steps: dict[str, dict] = field(default_factory=dict)   # step id -> {status, at, note, result, approved_by}
    inputs: dict[str, str] = field(default_factory=dict)   # non-secret inputs, e.g. file paths

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Runner:
    def __init__(self, root: Path | None = None, playbooks: dict[str, Playbook] | None = None,
                 env=os.environ.get, fetch: act.HttpFetcher = act.http_get, page_fetcher=None):
        self.root = Path(root or DEFAULT_DIR)
        self.playbooks = playbooks if playbooks is not None else load_all()
        self.env, self.fetch, self.page_fetcher = env, fetch, page_fetcher

    # --- storage ------------------------------------------------------------------------------
    def _path(self, mid: str) -> Path:
        return self.root / f"{mid}.json"

    def workdir(self, m: Migration) -> Path:
        d = self.root / m.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, m: Migration) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(m.id).write_text(json.dumps(m.to_dict(), indent=2))

    def load(self, mid: str) -> Migration:
        p = self._path(mid)
        if not p.is_file():
            raise MigrationError(f"no migration {mid}. See `swapstack migrate list`.")
        return Migration(**json.loads(p.read_text()))

    def all(self) -> list[Migration]:
        return [Migration(**json.loads(p.read_text())) for p in sorted(self.root.glob("*.json"))] if self.root.exists() else []

    # --- lifecycle ----------------------------------------------------------------------------
    def start(self, store: str, playbook_id: str, variant: str) -> Migration:
        pb = self.playbooks.get(playbook_id)
        if not pb:
            raise MigrationError(f"unknown playbook {playbook_id}; have {', '.join(self.playbooks)}")
        if variant not in pb.variants:
            raise MigrationError(f"{playbook_id} moves from {', '.join(pb.variants)}, not {variant}")
        host = normalize_store(store).split("//")[-1].removeprefix("www.")
        mid = f"{host.replace('.', '-')}--{variant}-to-{pb.to_catalog_id or pb.id.split('-to-')[-1]}"
        if self._path(mid).exists():
            return self.load(mid)
        m = Migration(id=mid, playbook=pb.id, variant=variant, store=host, created=_now(),
                      steps={s.id: {"status": "pending"} for s in pb.steps_for(variant)})
        self.save(m)
        return m

    def pb(self, m: Migration) -> Playbook:
        return self.playbooks[m.playbook]

    def steps(self, m: Migration) -> list[Step]:
        return self.pb(m).steps_for(m.variant)

    def _step(self, m: Migration, sid: str) -> Step:
        for s in self.steps(m):
            if s.id == sid:
                return s
        raise MigrationError(f"no step {sid} in this migration")

    def current(self, m: Migration) -> Step | None:
        for s in self.steps(m):
            if m.steps[s.id]["status"] not in DONE:
                return s
        return None

    def _check_ready(self, m: Migration, step: Step) -> None:
        for s in self.steps(m):
            if s.id == step.id:
                break
            st = m.steps[s.id]["status"]
            if s.gate and st not in DONE:
                raise MigrationError(f"'{s.title}' ({s.id}) has to be done first")
            if step.cancels_old and st not in DONE:
                raise MigrationError(f"every step before cancelling the old tool must be done or skipped; '{s.title}' is {st}")

    # --- context for text and actions -----------------------------------------------------------
    def context(self, m: Migration) -> dict:
        pb = self.pb(m)
        v = pb.variants[m.variant]
        results = {sid: (st.get("result") or {}).get("data", {}) for sid, st in m.steps.items()}
        return {"store": m.store, "from_name": v.get("name", m.variant), "to_name": pb.to, "v": v,
                "r": results, "inputs": m.inputs, "workdir": str(self.workdir(m))}

    def describe(self, m: Migration, step: Step) -> dict:
        ctx = self.context(m)
        out = {
            "migration": m.id, "step": step.id, "kind": step.kind, "title": fill(step.title, ctx),
            "instructions": fill(step.instructions, ctx), "status": m.steps[step.id]["status"],
            "optional": step.optional, "irreversible": step.irreversible, "cancels_old": step.cancels_old,
            "runnable": bool(step.action),
        }
        if step.waits:
            out["waits"] = fill(step.waits, ctx)
        if step.inputs:
            out["inputs"] = [{**i, "have": bool(m.inputs.get(i["name"]))} for i in step.inputs]
        if step.env:
            out["env"] = {n: ("set" if self.env(n) else "missing") for n in self.env_names(m, step)}
        if step.message:
            msg = self.pb(m).messages[step.message]
            out["message"] = {"to": fill(msg.to, ctx), "subject": fill(msg.subject, ctx), "body": fill(msg.body, ctx)}
        if step.kind == "approve" and step.id == self.steps(m)[0].id:
            out["loses"] = [fill(x, ctx) for x in self.pb(m).losses_for(m.variant)]
        if step.cancels_old and self.pb(m).rollback:
            out["rollback"] = fill(self.pb(m).rollback, ctx)
        res = m.steps[step.id].get("result")
        if res:
            out["result"] = res
        return out

    def env_names(self, m: Migration, step: Step) -> list[str]:
        """Env vars a step needs; "$variant" expands to the variant's own list (e.g. Gorgias vs Zendesk keys)."""
        names: list[str] = []
        for n in step.env:
            names += self.pb(m).variants[m.variant].get("env", []) if n == "$variant" else [n]
        return names

    def status(self, m: Migration) -> dict:
        cur = self.current(m)
        return {
            "migration": m.id, "playbook": m.playbook, "store": m.store, "from": m.variant,
            "steps": [{"step": s.id, "kind": s.kind, "title": fill(s.title, self.context(m)),
                       "status": m.steps[s.id]["status"], "optional": s.optional} for s in self.steps(m)],
            "next": self.describe(m, cur) if cur else None,
            "finished": cur is None,
        }

    # --- moving forward -----------------------------------------------------------------------
    def set_input(self, m: Migration, name: str, value: str) -> None:
        if any(k in name.lower() for k in ("token", "key", "secret", "password")):
            raise MigrationError("keys and tokens go in environment variables, not migration inputs")
        m.inputs[name] = value
        self.save(m)

    def run(self, m: Migration, sid: str) -> dict:
        step = self._step(m, sid)
        if not step.action:
            raise MigrationError(f"{sid} is a {step.kind} step with nothing to run; complete it once it's done")
        self._check_ready(m, step)
        missing = [i["name"] for i in step.inputs if not i.get("optional") and not m.inputs.get(i["name"])]
        if missing:
            raise MigrationError(f"{sid} needs: {', '.join(missing)} (set with `--input name=value`)")
        ctx = act.Context(
            store=m.store, variant=m.variant, workdir=self.workdir(m),
            results={k: (v.get("result") or {}).get("data", {}) for k, v in m.steps.items()},
            env=self.env, fetch=self.fetch,
            params={**step.params, **({"_page_fetcher": self.page_fetcher} if self.page_fetcher else {})},
        )
        try:
            result = act.ACTIONS[step.action](ctx, {i["name"]: m.inputs.get(i["name"]) for i in step.inputs})
        except (FileNotFoundError, KeyError) as e:
            result = act._result(False, f"Couldn't read an input: {e}")
        m.steps[sid] = {**m.steps[sid], "status": "done" if result["ok"] else "failed", "at": _now(), "result": result}
        self.save(m)
        return result

    def complete(self, m: Migration, sid: str, note: str = "", approved_by: str = "") -> None:
        step = self._step(m, sid)
        self._check_ready(m, step)
        if step.kind == "approve" and not approved_by:
            raise MigrationError(f"'{step.title}' needs the merchant's explicit yes. Ask them, then pass who approved.")
        if step.kind == "auto" and m.steps[sid]["status"] != "done":
            raise MigrationError(f"{sid} is automatic: use run, not complete")
        if step.kind == "verify" and step.action and m.steps[sid]["status"] != "done":
            raise MigrationError(f"{sid} is a check: run it and let it pass")
        m.steps[sid] = {**m.steps[sid], "status": "done", "at": _now(), "note": note,
                        **({"approved_by": approved_by} if approved_by else {})}
        self.save(m)

    def wait(self, m: Migration, sid: str, note: str = "") -> None:
        step = self._step(m, sid)
        self._check_ready(m, step)
        m.steps[sid] = {**m.steps[sid], "status": "waiting", "at": _now(), "note": note}
        self.save(m)

    def skip(self, m: Migration, sid: str, reason: str) -> None:
        step = self._step(m, sid)
        if not step.optional:
            raise MigrationError(f"{sid} isn't optional")
        m.steps[sid] = {**m.steps[sid], "status": "skipped", "at": _now(), "note": reason}
        self.save(m)

    def draft(self, m: Migration, sid: str) -> Path:
        """Write a message step's draft to the work folder for the merchant to review and send."""
        d = self.describe(m, self._step(m, sid))
        if "message" not in d:
            raise MigrationError(f"{sid} has no message")
        p = self.workdir(m) / f"message-{sid}.md"
        msg = d["message"]
        p.write_text(f"To: {msg['to']}\nSubject: {msg['subject']}\n\n{msg['body']}\n")
        return p
