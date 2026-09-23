"""Theme scan: read a downloaded Shopify theme (folder or .zip) for app code and leftovers.

Get the file from Shopify admin > Online Store > Themes > ... > Download theme file.

Finds:
  - app blocks and app embeds in templates and config/settings_data.json, including disabled ones
  - app script URLs hard-coded into Liquid, JS or CSS
  - snippets that belong to known apps
  - orphan snippets that nothing renders any more, the usual residue of an uninstalled app
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .fingerprints import Catalog
from .scan import RE_APP_BLOCK, RE_INLINE_URL, Evidence

TEXT_EXT = {".liquid", ".json", ".js", ".css", ".scss"}
RE_RENDER = re.compile(r"""\{%-?\s*(?:render|include)\s+['"]([^'"]+)['"]""")
RE_SECTION = re.compile(r"""\{%-?\s*section\s+['"]([^'"]+)['"]""")
RE_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


@dataclass
class ThemeResult:
    source: str
    files: int = 0
    detections: dict[str, list[Evidence]] = field(default_factory=dict)
    app_embeds: list[dict] = field(default_factory=list)       # {handle, block, disabled, app_id}
    orphan_snippets: list[dict] = field(default_factory=list)  # {name, bytes, app_id}
    unknown_handles: list[str] = field(default_factory=list)

    def add(self, app_id: str, ev: Evidence) -> None:
        bucket = self.detections.setdefault(app_id, [])
        if not any(e.source == ev.source and e.detail == ev.detail for e in bucket):
            bucket.append(ev)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "files": self.files,
            "detections": {k: [e.to_dict() for e in v] for k, v in self.detections.items()},
            "app_embeds": self.app_embeds,
            "orphan_snippets": self.orphan_snippets,
            "unknown_handles": self.unknown_handles,
        }


def _read_theme(path: Path) -> dict[str, str]:
    """Return {relative/posix/path: text} for every text file in the theme."""
    files: dict[str, str] = {}
    if path.is_dir():
        for f in path.rglob("*"):
            if f.is_file() and f.suffix in TEXT_EXT:
                files[f.relative_to(path).as_posix()] = f.read_text(errors="replace")
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if PurePosixPath(name).suffix in TEXT_EXT and not name.endswith("/"):
                    files[name] = z.read(name).decode("utf-8", errors="replace")
    else:
        raise ValueError(f"{path} is neither a theme folder nor a .zip")
    return _strip_root(files)


def _strip_root(files: dict[str, str]) -> dict[str, str]:
    """Zips often wrap the theme in one top-level folder. Drop it so paths start at layout/, snippets/..."""
    roots = {p.split("/", 1)[0] for p in files if "/" in p}
    known = {"layout", "templates", "sections", "snippets", "assets", "config", "locales", "blocks"}
    if len(roots) == 1 and not roots & known:
        root = roots.pop() + "/"
        return {p[len(root):]: t for p, t in files.items() if p.startswith(root)}
    return files


def _walk_blocks(node, found: list[dict]) -> None:
    if isinstance(node, dict):
        t = node.get("type")
        if isinstance(t, str) and t.startswith("shopify://apps/"):
            m = RE_APP_BLOCK.search(t)
            if m:
                found.append({"handle": m.group(1).lower(), "block": m.group(2), "disabled": bool(node.get("disabled"))})
        for v in node.values():
            _walk_blocks(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_blocks(v, found)


def scan_theme(path: str | Path, catalog: Catalog) -> ThemeResult:
    path = Path(path).expanduser()
    files = _read_theme(path)
    result = ThemeResult(source=str(path), files=len(files))

    # App blocks and embeds, with their enabled/disabled state from JSON.
    seen: set[tuple[str, str, bool]] = set()
    for rel, text in files.items():
        if not rel.endswith(".json"):
            continue
        try:
            data = json.loads(RE_BLOCK_COMMENT.sub("", text))
        except json.JSONDecodeError:
            continue
        found: list[dict] = []
        _walk_blocks(data, found)
        for b in found:
            key = (b["handle"], b["block"], b["disabled"])
            if key in seen:
                continue
            seen.add(key)
            app = catalog.match_key(b["handle"])
            b["app_id"] = app.id if app else None
            b["file"] = rel
            result.app_embeds.append(b)
            if app and not b["disabled"]:
                result.add(app.id, Evidence("theme-block", f"{b['handle']}/{b['block']}", rel))
            elif not app and b["handle"] not in result.unknown_handles:
                result.unknown_handles.append(b["handle"])

    # Which snippets does anything still render? Orphans are dead code, and their URLs don't count as live.
    referenced: set[str] = set()
    for rel, text in files.items():
        if rel.endswith(".liquid"):
            referenced.update(RE_RENDER.findall(text))
            referenced.update(RE_SECTION.findall(text))
        elif rel.endswith(".json"):
            referenced.update(re.findall(r'"type"\s*:\s*"([a-z0-9_-]+)"', text))
    orphans: set[str] = set()
    for rel, text in files.items():
        if not rel.startswith("snippets/") or not rel.endswith(".liquid"):
            continue
        name = PurePosixPath(rel).stem
        app = catalog.match_key(name)
        if name in referenced:
            if app:
                result.add(app.id, Evidence("theme-snippet", rel, rel))
        else:
            orphans.add(rel)
            if not app:  # attribute an unnamed snippet by the app URLs inside it
                for u in RE_INLINE_URL.findall(text):
                    app = catalog.match_url(u)
                    if app:
                        break
            result.orphan_snippets.append({"name": rel, "bytes": len(text.encode()), "app_id": app.id if app else None})

    # Hard-coded app URLs in live code.
    for rel, text in files.items():
        if rel.endswith(".json") or rel in orphans:
            continue
        for u in RE_INLINE_URL.findall(text):
            app = catalog.match_url(u)
            if app:
                result.add(app.id, Evidence("theme-code", re.sub(r"^(https?:)?//", "", u.split("?")[0])[:90], rel))

    return result
