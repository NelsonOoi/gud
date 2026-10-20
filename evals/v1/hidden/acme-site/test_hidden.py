"""Hidden acceptance tests for the acme-site spec. Agents never see this file.

The harness runs them against a copy of the final tree, named by EVAL_TREE.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

TREE = Path(os.environ.get("EVAL_TREE", ".")).resolve()
PAGES = ("index.html", "pricing.html", "about.html")


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.title = ""
        self.links: list[dict] = []        # <link>
        self.anchors: list[dict] = []      # <a>, with "in_nav" and "text"
        self.navs: list[list[str]] = []    # hrefs per <nav>
        self.classes: list[tuple[str, str]] = []   # (tag, "class id")
        self.table_text: list[str] = []
        self.tables = 0
        self.table_rows = 0
        self.text: list[str] = []
        self._in_title = False
        self._nav_depth = 0
        self._table_depth = 0
        self._anchor: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = {k: (v or "") for k, v in attrs}
        names = f"{attrs.get('class', '')} {attrs.get('id', '')}".lower()
        self.classes.append((tag, names))
        if tag == "title":
            self._in_title = True
        elif tag == "link":
            self.links.append(attrs)
        elif tag == "nav":
            self._nav_depth += 1
            self.navs.append([])
        elif tag == "table":
            self._table_depth += 1
            self.tables += 1
        elif tag == "tr" and self._table_depth:
            self.table_rows += 1
        elif tag == "a":
            href = attrs.get("href", "")
            self._anchor = {"href": href, "in_nav": self._nav_depth > 0, "text": ""}
            self.anchors.append(self._anchor)
            if self._nav_depth and self.navs:
                self.navs[-1].append(_norm(href))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "nav" and self._nav_depth:
            self._nav_depth -= 1
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
        elif tag == "a":
            self._anchor = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._table_depth:
            self.table_text.append(data)
        if self._anchor is not None:
            self._anchor["text"] += data
        self.text.append(data)


def _norm(href: str) -> str:
    href = (href or "").strip()
    href = href.split("#", 1)[0].split("?", 1)[0]
    if href.startswith("./"):
        href = href[2:]
    if href.startswith("/"):
        href = href[1:]
    return href or "index.html"


def _read(name: str) -> str:
    path = TREE / name
    if not path.is_file():
        pytest.fail(f"{name} is missing")
    return path.read_text(encoding="utf-8", errors="replace")


def _page(name: str) -> Page:
    parser = Page()
    parser.feed(_read(name))
    return parser


@pytest.mark.parametrize("name", PAGES + ("styles.css",))
def test_file_exists(name):
    assert (TREE / name).is_file(), f"{name} is missing"


@pytest.mark.parametrize("name", PAGES)
def test_page_basics(name):
    text = _read(name)
    assert re.match(r"\s*<!doctype html>", text, re.I), "page must start with <!doctype html>"
    page = _page(name)
    assert page.title.strip(), "page needs a non-empty <title>"
    sheets = [l for l in page.links if "stylesheet" in l.get("rel", "").lower()]
    assert any(_norm(l.get("href", "")) == "styles.css" for l in sheets), "page must link styles.css"


@pytest.mark.parametrize("name", PAGES)
def test_nav_links_every_page(name):
    page = _page(name)
    assert page.navs, "page needs a <nav>"
    hrefs = set(page.navs[0])
    for target in PAGES:
        assert target in hrefs, f"nav must link {target}"


def test_nav_shared():
    navs = [tuple(_page(name).navs[0]) if _page(name).navs else () for name in PAGES]
    assert navs[0], "index.html needs a <nav>"
    assert all(nav == navs[0] for nav in navs), f"every page must share the same nav links: {navs}"


def test_index_hero():
    page = _page("index.html")
    assert any("hero" in names for _tag, names in page.classes), "index.html needs a hero (class or id 'hero')"


def test_index_three_feature_cards():
    page = _page("index.html")
    # An element is a card when one of its class/id tokens ends in "card" or "feature"
    # (card, feature-card, features__card, feature). Containers ("cards", "features")
    # and parts ("card-title") do not count.
    token = re.compile(r"^[\w-]*(card|feature)$")
    own = [names for _tag, names in page.classes if any(token.match(t) for t in names.split())]
    assert len(own) >= 3, f"index.html needs three feature cards, found {len(own)}"


def test_index_cta_links_pricing():
    page = _page("index.html")
    cta = [a for a in page.anchors if not a["in_nav"] and _norm(a["href"]) == "pricing.html"]
    assert cta, "index.html needs a call to action (outside the nav) linking pricing.html"


def test_pricing_is_a_real_table():
    page = _page("pricing.html")
    assert page.tables >= 1, "pricing.html must use a <table>"
    assert page.table_rows >= 2, "the pricing table needs rows"


def test_pricing_plans_and_prices():
    page = _page("pricing.html")
    text = " ".join(" ".join(page.table_text).split())
    for word in ("Free", "Pro", "Team"):
        assert word in text, f"pricing table must name the {word} plan"
    assert re.search(r"\$\s*8\b", text), "Pro must cost $8"
    assert re.search(r"\$\s*15\b", text), "Team must cost $15"
    assert re.search(r"/\s*user", text, re.I), "Team is priced per user"
    assert re.search(r"/\s*mo", text, re.I), "prices are per month"


def test_about_mailto():
    page = _page("about.html")
    hrefs = [a["href"].strip() for a in page.anchors]
    assert "mailto:hello@acme.example" in hrefs, "about.html needs a mailto:hello@acme.example link"


def test_about_story():
    page = _page("about.html")
    words = " ".join(page.text).split()
    assert len(words) >= 30, "about.html needs a short company story"


def test_styles_cover_components():
    css = _read("styles.css").lower()
    assert "nav" in css, "styles.css must style the nav"
    assert "table" in css or "pricing" in css, "styles.css must style the pricing table"
    assert ".hero" in css or "#hero" in css, "styles.css must style the hero"
    assert "card" in css, "styles.css must style the cards"


def test_styles_responsive_under_700px():
    css = _read("styles.css").lower()
    assert re.search(r"@media[^{]*max-width\s*:\s*(6\d\d|700)px", css), "styles.css needs a max-width ~700px media query"


def test_local_links_resolve():
    for name in PAGES:
        page = _page(name)
        for a in page.anchors:
            href = a["href"].strip()
            if not href or href.startswith(("http:", "https:", "mailto:", "#", "tel:")):
                continue
            assert (TREE / _norm(href)).is_file(), f"{name} links missing {href}"
