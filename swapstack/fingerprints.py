"""App fingerprint catalog: maps script URLs, app handles and bill descriptions to known apps."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


def norm(text: str) -> str:
    """Lowercase and strip everything but letters and digits, for fuzzy key matching."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass(frozen=True)
class App:
    id: str
    name: str
    category: str
    urls: tuple[str, ...]
    keys: tuple[str, ...]
    storefront: bool = True  # False for back-office apps that leave no storefront footprint
    free: bool = False       # True for apps with no paid tier
    optional: bool = False   # True when the app often runs without any storefront code (helpdesk, email, fraud)
    exact: bool = False      # keys only match the whole handle or name, for generic words like 'inbox'


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    exclusive: bool          # True when running two apps here is almost always redundant
    free_option: str | None


class Catalog:
    def __init__(self, data: dict):
        self.version: str = data.get("version", "unknown")
        self.categories: dict[str, Category] = {
            cid: Category(cid, c["label"], bool(c.get("exclusive")), c.get("free_option"))
            for cid, c in data["categories"].items()
        }
        self.apps: dict[str, App] = {}
        for a in data["apps"]:
            if a["category"] not in self.categories:
                raise ValueError(f"app {a['id']} has unknown category {a['category']}")
            self.apps[a["id"]] = App(
                id=a["id"],
                name=a["name"],
                category=a["category"],
                urls=tuple(u.lower() for u in a.get("urls", [])),
                keys=tuple(norm(k) for k in a.get("keys", [])),
                storefront=a.get("storefront", True),
                free=a.get("free", False),
                optional=a.get("optional", False),
                exact=a.get("exact", False),
            )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Catalog":
        if path:
            return cls(json.loads(Path(path).read_text()))
        text = resources.files("swapstack").joinpath("data/apps.json").read_text()
        return cls(json.loads(text))

    def category(self, app: App | None) -> Category | None:
        return self.categories.get(app.category) if app else None

    def match_url(self, url: str) -> App | None:
        """Longest matching URL pattern wins, so 'cdn-loyalty.yotpo.com' beats 'yotpo.com'."""
        u = url.lower()
        best: tuple[int, App] | None = None
        for app in self.apps.values():
            for pat in app.urls:
                if pat and pat in u and (best is None or len(pat) > best[0]):
                    best = (len(pat), app)
        return best[1] if best else None

    def match_key(self, text: str) -> App | None:
        """Match an app handle, snippet filename or bill description. Longest key wins."""
        n = norm(text)
        if not n:
            return None
        best: tuple[int, App] | None = None
        for app in self.apps.values():
            for key in app.keys:
                hit = (key == n) if app.exact else (key in n)
                if key and hit and (best is None or len(key) > best[0]):
                    best = (len(key), app)
        return best[1] if best else None
