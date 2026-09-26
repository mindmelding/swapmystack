"""Storefront scan: read the public HTML a Shopify store serves and find the apps it loads.

Signals, strongest first:
  - app blocks and app embeds that Shopify marks in HTML comments and extension asset paths
  - ScriptTags that Shopify injects through the asyncLoad() bootstrap
  - script, iframe, link, img and inline-script URLs that point at known app CDNs

Public pages only. One request per page, no retries, no evasion.
"""

from __future__ import annotations

import gzip
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlparse

from . import __version__
from .fingerprints import Catalog

USER_AGENT = f"swapstack/{__version__} (Shopify app spend audit; public pages only)"
MAX_BYTES = 8_000_000

Fetcher = Callable[[str], "Page"]

RE_ASYNCLOAD = re.compile(r"function\s+asyncLoad\s*\(\)\s*\{.*?var\s+urls\s*=\s*(\[.*?\])\s*;", re.S)
RE_QUOTED = re.compile(r"""["']((?:https?:)?\\?/\\?/[^"'\s]+)["']""")
RE_EXTENSION = re.compile(r"/extensions/[0-9a-f-]{8,}/([a-z0-9][a-z0-9-]*?)-\d+/", re.I)
RE_APP_BLOCK = re.compile(r"shopify://apps/([a-z0-9-]+)/blocks/([a-z0-9_-]+)", re.I)
RE_INLINE_URL = re.compile(r"""(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}(?:/[^"'\s<>)]*)?""", re.I)

# First-party and platform hosts that never indicate a third-party app on their own.
PLATFORM_HOSTS = (
    "cdn.shopify.com", "shopify.com", "myshopify.com", "shop.app", "shopifycdn.com",
    "shopifysvc.com", "monorail-edge.shopifysvc.com",
)


@dataclass
class Page:
    url: str
    status: int
    html: str
    error: str | None = None


@dataclass
class Evidence:
    source: str   # "app-block", "app-embed", "scripttag", "script", "inline"
    detail: str
    page: str = ""

    def to_dict(self) -> dict:
        return {"source": self.source, "detail": self.detail, "page": self.page}


@dataclass
class ScanResult:
    store: str
    pages: list[dict] = field(default_factory=list)
    is_shopify: bool = False
    detections: dict[str, list[Evidence]] = field(default_factory=dict)
    unknown_handles: dict[str, list[str]] = field(default_factory=dict)
    third_party_hosts: dict[str, int] = field(default_factory=dict)

    def add(self, app_id: str, ev: Evidence) -> None:
        bucket = self.detections.setdefault(app_id, [])
        if not any(e.source == ev.source and e.detail == ev.detail for e in bucket):
            bucket.append(ev)

    def to_dict(self) -> dict:
        return {
            "store": self.store,
            "is_shopify": self.is_shopify,
            "pages": self.pages,
            "detections": {k: [e.to_dict() for e in v] for k, v in self.detections.items()},
            "unknown_handles": self.unknown_handles,
            "third_party_hosts": self.third_party_hosts,
        }


def normalize_store(store: str) -> str:
    s = store.strip()
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    p = urlparse(s)
    return f"https://{p.netloc.lower()}"


def http_fetch(url: str, timeout: float = 20.0) -> Page:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            charset = resp.headers.get_content_charset() or "utf-8"
            return Page(resp.geturl(), resp.status, raw.decode(charset, errors="replace"))
    except urllib.error.HTTPError as e:
        return Page(url, e.code, "", error=f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Page(url, 0, "", error=str(getattr(e, "reason", e)))


class _AssetParser(HTMLParser):
    """Collects asset URLs (not navigation links) and inline script bodies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.assets: list[tuple[str, str]] = []   # (kind, url)
        self.links: list[str] = []                # <a href>, used only to find a product page
        self.inline: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            if a.get("src"):
                self.assets.append(("script", a["src"]))
            else:
                self._in_script = True
        elif tag in ("iframe", "img", "source") and a.get("src"):
            self.assets.append((tag, a["src"]))
        elif tag == "link" and a.get("href") and (a.get("rel") or "").lower() in (
            "stylesheet", "preload", "modulepreload", "prefetch", "preconnect", "dns-prefetch"
        ):
            self.assets.append(("link", a["href"]))
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.inline.append(data)


def _host(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    return urlparse(url).netloc.lower()


def _is_platform(host: str, store_host: str) -> bool:
    bare = store_host.removeprefix("www.")
    return (not host) or host.endswith(bare) or any(host == h or host.endswith("." + h) for h in PLATFORM_HOSTS)


def extract(html: str, page_url: str, catalog: Catalog, result: ScanResult) -> list[str]:
    """Record every app signal in one page. Returns in-store links for page discovery."""
    store_host = _host(result.store)
    label = urlparse(page_url).path or "/"

    if "cdn.shopify.com" in html or "Shopify.shop" in html or "shopify-features" in html:
        result.is_shopify = True

    for handle, block in RE_APP_BLOCK.findall(html):
        _record_handle(handle.lower(), f"{handle}/{block}", "app-block", label, catalog, result)
    for handle in RE_EXTENSION.findall(html):
        _record_handle(handle.lower(), handle, "app-embed", label, catalog, result)

    m = RE_ASYNCLOAD.search(html)
    if m:
        for u in RE_QUOTED.findall(m.group(1)):
            u = u.replace("\\/", "/")
            app = catalog.match_url(u)
            if app:
                result.add(app.id, Evidence("scripttag", _short(u), label))
            else:
                _count_host(u, store_host, result)

    parser = _AssetParser()
    try:
        parser.feed(html)
    except Exception:  # malformed HTML should never sink a scan
        pass

    for kind, u in parser.assets:
        if _is_first_party(u, store_host):
            continue  # the store's own theme files: a name like "hollowpeak" must not match a vendor
        app = catalog.match_url(u)
        if app:
            result.add(app.id, Evidence("script" if kind == "script" else kind, _short(u), label))
        elif kind == "script":
            _count_host(u, store_host, result)

    for body in parser.inline:
        for u in RE_INLINE_URL.findall(body.replace("\\/", "/")):
            if _is_first_party(u, store_host):
                continue
            app = catalog.match_url(u)
            if app:
                result.add(app.id, Evidence("inline", _short(u), label))

    return [urljoin(page_url, h) for h in parser.links]


def _record_handle(handle: str, detail: str, source: str, label: str, catalog: Catalog, result: ScanResult) -> None:
    if handle[:1].isdigit():
        return  # versioned asset folders like "2026-09-22-08-46-47-utc-" are not app handles
    app = catalog.match_key(handle)
    if app:
        result.add(app.id, Evidence(source, detail, label))
    else:
        pages = result.unknown_handles.setdefault(handle, [])
        if label not in pages:
            pages.append(label)


def _is_first_party(url: str, store_host: str) -> bool:
    host = _host(url)
    bare = store_host.removeprefix("www.")
    return bool(host) and (host == bare or host.endswith("." + bare))


def _count_host(url: str, store_host: str, result: ScanResult) -> None:
    host = _host(url)
    if host and not _is_platform(host, store_host):
        result.third_party_hosts[host] = result.third_party_hosts.get(host, 0) + 1


def _short(url: str) -> str:
    u = url.split("?")[0]
    if u.startswith("//"):
        u = u[2:]
    u = re.sub(r"^https?://", "", u)
    return u if len(u) <= 90 else u[:87] + "..."


def _pick_pages(home: str, links: list[str], limit: int) -> list[str]:
    """Home, then one product and one collection page. Apps often load only on product pages."""
    host = _host(home)
    picks: list[str] = []
    for prefix in ("/products/", "/collections/", "/cart"):
        for link in links:
            p = urlparse(link)
            if p.netloc.lower() == host and p.path.startswith(prefix) and p.path.rstrip("/") != "/collections":
                url = f"https://{host}{p.path}"
                if url not in picks:
                    picks.append(url)
                    break
    return picks[: max(0, limit - 1)]


def scan_storefront(store: str, catalog: Catalog, pages: int = 3, fetcher: Fetcher | None = None) -> ScanResult:
    fetch = fetcher or http_fetch
    base = normalize_store(store)
    result = ScanResult(store=base)

    home = fetch(base + "/")
    result.pages.append({"url": home.url, "status": home.status, "error": home.error})
    if not home.html:
        return result
    result.store = normalize_store(home.url)  # follow redirects to the canonical domain
    links = extract(home.html, home.url, catalog, result)

    for url in _pick_pages(result.store + "/", links, pages):
        page = fetch(url)
        result.pages.append({"url": page.url, "status": page.status, "error": page.error})
        if page.html:
            extract(page.html, page.url, catalog, result)
    return result
