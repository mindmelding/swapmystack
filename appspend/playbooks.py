"""Migration playbooks: one JSON file per switch (e.g. reviews → Judge.me), shipped in data/playbooks/.

A playbook is data, not code. Any harness (Claude Code, Cursor, ChatGPT, a plain terminal) walks the
same steps through the CLI or the MCP server. See docs/playbooks.md for the conventions.
"""

from __future__ import annotations

import json
import string
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

# What kind of work a step is, and who does it.
KINDS = {
    "approve",          # the merchant says yes to something; never completed without explicit approval
    "message",          # a draft to a vendor; the merchant reviews and sends it from their own account
    "merchant_action",  # the merchant clicks something in an admin we can't reach
    "agent",            # the AI assistant does it with tools the harness has (e.g. a vendor MCP), merchant reviews
    "auto",             # appspend runs it (an action in actions.py)
    "vendor",           # waiting on the vendor's team (e.g. they load a file)
    "verify",           # a check that must pass before anything irreversible; auto if it has an action
}
GATES = {"approve", "verify"}


@dataclass(frozen=True)
class Step:
    id: str
    kind: str
    title: str
    instructions: str
    action: str = ""
    params: dict = field(default_factory=dict)
    inputs: tuple[dict, ...] = ()
    env: tuple[str, ...] = ()
    message: str = ""
    when: tuple[str, ...] = ()        # variants this step applies to; empty = all
    optional: bool = False
    irreversible: bool = False
    cancels_old: bool = False
    waits: str = ""                   # who or what we wait on, and roughly how long

    @property
    def gate(self) -> bool:
        return self.kind in GATES


@dataclass(frozen=True)
class Message:
    id: str
    to: str
    subject: str
    body: str


@dataclass(frozen=True)
class Playbook:
    id: str
    title: str
    category: str
    to: str
    to_catalog_id: str
    summary: str
    checked: str
    variants: dict[str, dict]
    loses: dict[str, tuple[str, ...]]
    mapping: tuple[dict, ...]
    messages: dict[str, Message]
    steps: tuple[Step, ...]
    rollback: str
    sources: tuple[str, ...]

    def steps_for(self, variant: str) -> list[Step]:
        return [s for s in self.steps if not s.when or variant in s.when]

    def losses_for(self, variant: str) -> list[str]:
        return list(self.loses.get("all", ())) + list(self.loses.get(variant, ()))


def _parse(d: dict) -> Playbook:
    steps = tuple(
        Step(
            id=s["id"], kind=s["kind"], title=s["title"], instructions=s.get("instructions", ""),
            action=s.get("action", ""), params=s.get("params") or {}, inputs=tuple(s.get("inputs") or ()),
            env=tuple(s.get("env") or ()), message=s.get("message", ""), when=tuple(s.get("when") or ()),
            optional=bool(s.get("optional")), irreversible=bool(s.get("irreversible")),
            cancels_old=bool(s.get("cancels_old")), waits=s.get("waits", ""),
        )
        for s in d["steps"]
    )
    messages = {k: Message(k, m["to"], m["subject"], m["body"]) for k, m in (d.get("messages") or {}).items()}
    return Playbook(
        id=d["id"], title=d["title"], category=d["category"], to=d["to"], to_catalog_id=d.get("to_catalog_id", ""),
        summary=d["summary"], checked=d["checked"], variants=d["variants"],
        loses={k: tuple(v) for k, v in (d.get("loses") or {}).items()}, mapping=tuple(d.get("mapping") or ()),
        messages=messages, steps=steps, rollback=d.get("rollback", ""), sources=tuple(d.get("sources") or ()),
    )


def validate(pb: Playbook, catalog_ids: set[str] | None = None, action_names: set[str] | None = None) -> list[str]:
    """Problems with a playbook; empty when it is sound."""
    errs: list[str] = []
    ids = [s.id for s in pb.steps]
    if len(ids) != len(set(ids)):
        errs.append("duplicate step ids")
    for s in pb.steps:
        where = f"{pb.id}/{s.id}"
        if s.kind not in KINDS:
            errs.append(f"{where}: unknown kind {s.kind}")
        if s.kind == "auto" and not s.action:
            errs.append(f"{where}: auto step needs an action")
        if s.action and action_names is not None and s.action not in action_names:
            errs.append(f"{where}: unknown action {s.action}")
        if s.kind == "message" and s.message not in pb.messages:
            errs.append(f"{where}: unknown message {s.message}")
        for v in s.when:
            if v not in pb.variants:
                errs.append(f"{where}: unknown variant {v}")
        if s.irreversible and s.kind != "approve":
            errs.append(f"{where}: irreversible steps must be approve steps")
        for inp in s.inputs:
            if inp.get("type") == "secret":
                errs.append(f"{where}: secrets go in env vars, never step inputs")
    for variant in pb.variants:
        steps = pb.steps_for(variant)
        cancel = [i for i, s in enumerate(steps) if s.cancels_old]
        verify = [i for i, s in enumerate(steps) if s.kind == "verify"]
        approve = [i for i, s in enumerate(steps) if s.kind == "approve" and s.irreversible]
        if not cancel:
            errs.append(f"{pb.id} ({variant}): no step cancels the old tool")
            continue
        if not verify or verify[0] > cancel[0]:
            errs.append(f"{pb.id} ({variant}): cancelling the old tool must come after a verify step")
        if not any(a < cancel[0] for a in approve):
            errs.append(f"{pb.id} ({variant}): cancelling the old tool needs an irreversible approve step first")
        if not steps or steps[0].kind != "approve":
            errs.append(f"{pb.id} ({variant}): the first step must be the merchant approving the plan")
    if catalog_ids is not None:
        for v in pb.variants:
            if v not in catalog_ids:
                errs.append(f"{pb.id}: variant {v} is not a catalog app id")
        if pb.to_catalog_id and pb.to_catalog_id not in catalog_ids:
            errs.append(f"{pb.id}: to_catalog_id {pb.to_catalog_id} is not a catalog app id")
    for text in [s.instructions for s in pb.steps] + [m.body for m in pb.messages.values()]:
        if "—" in text:
            errs.append(f"{pb.id}: em dash in copy")
    return errs


def load_all(path: str | Path | None = None) -> dict[str, Playbook]:
    if path:
        files = sorted(Path(path).glob("*.json"))
        texts = [f.read_text() for f in files]
    else:
        root = resources.files("appspend").joinpath("data/playbooks")
        texts = [f.read_text() for f in sorted(root.iterdir(), key=lambda f: f.name) if f.name.endswith(".json")]
    out: dict[str, Playbook] = {}
    for t in texts:
        pb = _parse(json.loads(t))
        out[pb.id] = pb
    return out


def for_app(app_id: str, playbooks: dict[str, Playbook] | None = None) -> Playbook | None:
    """The playbook that moves a store off this app, if one exists."""
    for pb in (playbooks if playbooks is not None else load_all()).values():
        if app_id in pb.variants:
            return pb
    return None


class _Lenient(string.Formatter):
    """str.format that leaves unknown fields as-is, so a missing result never breaks a walkthrough."""

    def get_field(self, field_name, args, kwargs):
        try:
            return super().get_field(field_name, args, kwargs)
        except (KeyError, IndexError, AttributeError, TypeError):
            return "{" + field_name + "}", field_name

    def format_field(self, value, format_spec):
        try:
            return super().format_field(value, format_spec)
        except (ValueError, TypeError):
            return str(value)


def fill(text: str, ctx: dict) -> str:
    return _Lenient().vformat(text, (), ctx)
