"""Hidden acceptance tests for the acme-site-shared spec. Agents never see this file.

The harness runs them against a copy of the final tree, named by EVAL_TREE. The site is built once with
`python build.py` (into TREE/site) and the built pages are checked.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

TREE = Path(os.environ.get("EVAL_TREE", ".")).resolve()
SITE = TREE / "site"
COMMITTED_SITE = SITE.exists()
PAGES = {"index.html": "Home", "features.html": "Features", "pricing.html": "Pricing", "about.html": "About",
         "contact.html": "Contact"}
ORDER = list(PAGES)


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[dict] = []
        self.anchors: list[dict] = []
        self.navs: list[list[dict]] = []
        self.elements: list[tuple[str, dict]] = []
        self.styles = 0
        self.text: list[str] = []
        self.table_text: list[str] = []
        self.stack: list[tuple[str, dict]] = []
        self._in_title = False
        self._nav_depth = 0
        self._table_depth = 0
        self._anchor: dict | None = None
        self.figcaptions: list[str] = []
        self._figcaption: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = {k: (v or "") for k, v in attrs}
        self.elements.append((tag, attrs))
        ancestors = [t for t, _ in self.stack]
        attrs["_ancestors"] = ancestors + []
        attrs["_classes_up"] = " ".join(a.get("class", "") for _, a in self.stack)
        if tag not in ("meta", "link", "input", "br", "img", "hr"):
            self.stack.append((tag, attrs))
        if tag == "title":
            self._in_title = True
        elif tag == "style":
            self.styles += 1
        elif tag == "link":
            self.links.append(attrs)
        elif tag == "nav":
            self._nav_depth += 1
            self.navs.append([])
        elif tag == "table":
            self._table_depth += 1
        elif tag == "figcaption":
            self._figcaption = []
        elif tag == "a":
            self._anchor = {"href": attrs.get("href", ""), "in_nav": self._nav_depth > 0, "text": "",
                            "current": attrs.get("aria-current"), "class": attrs.get("class", ""),
                            "up": attrs.get("_classes_up", "")}
            self.anchors.append(self._anchor)
            if self._nav_depth and self.navs:
                self.navs[-1].append(self._anchor)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag == "title":
            self._in_title = False
        elif tag == "nav" and self._nav_depth:
            self._nav_depth -= 1
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
        elif tag == "figcaption" and self._figcaption is not None:
            self.figcaptions.append(" ".join("".join(self._figcaption).split()))
            self._figcaption = None
        elif tag == "a":
            self._anchor = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._table_depth:
            self.table_text.append(data)
        if self._anchor is not None:
            self._anchor["text"] += data
        if self._figcaption is not None:
            self._figcaption.append(data)
        self.text.append(data)

    def with_class(self, tag: str | None, cls: str) -> list[dict]:
        return [a for t, a in self.elements if (tag is None or t == tag) and cls in a.get("class", "").split()]


def _norm(href: str) -> str:
    href = (href or "").strip().split("#", 1)[0].split("?", 1)[0]
    if href.startswith("./"):
        href = href[2:]
    if href.startswith("/"):
        href = href[1:]
    return href or "index.html"


@pytest.fixture(scope="session")
def built():
    proc = subprocess.run([sys.executable, "build.py"], cwd=TREE, capture_output=True, text=True, timeout=60)
    return proc


def _raw(name: str) -> str:
    path = SITE / name
    if not path.is_file():
        pytest.fail(f"site/{name} was not built")
    return path.read_text(encoding="utf-8", errors="replace")


def _page(name: str) -> Page:
    parser = Page()
    parser.feed(_raw(name))
    return parser


def test_build_runs(built):
    assert built.returncode == 0, built.stdout + built.stderr


def test_site_is_not_committed():
    assert not COMMITTED_SITE, "site/ is build output and must not be committed"


@pytest.mark.parametrize("name", ORDER)
def test_page_built(built, name):
    assert (SITE / name).is_file(), f"site/{name} was not built"


@pytest.mark.parametrize("name", ORDER)
def test_page_basics(built, name):
    text = _raw(name)
    assert re.match(r"\s*<!doctype html>", text, re.I), "page must start with <!doctype html>"
    page = _page(name)
    assert " ".join(page.title.split()) == f"{PAGES[name]} | Acme Notes"
    sheets = [_norm(l.get("href", "")) for l in page.links if "stylesheet" in l.get("rel", "").lower()]
    assert sheets == ["styles.css"], f"exactly one stylesheet, styles.css: {sheets}"
    assert page.styles == 0, "no <style> blocks"


@pytest.mark.parametrize("name", ORDER)
def test_nav_order_and_current(built, name):
    page = _page(name)
    assert len(page.navs) == 1, f"exactly one <nav> per page, found {len(page.navs)}"
    links = page.navs[0]
    assert [_norm(a["href"]) for a in links] == ORDER, "nav links Home, Features, Pricing, About, Contact in order"
    assert [" ".join(a["text"].split()) for a in links] == [PAGES[n] for n in ORDER], "nav link texts"
    current = [_norm(a["href"]) for a in links if a["current"] == "page"]
    assert current == [name], f"aria-current=page must be on the {name} link only, found {current}"


def test_nav_shared(built):
    def shape(name):
        return [(_norm(a["href"]), " ".join(a["text"].split())) for a in _page(name).navs[0]] if _page(name).navs else []
    shapes = [shape(name) for name in ORDER]
    assert shapes[0] and all(s == shapes[0] for s in shapes), "every page must share the same nav"


def test_nav_has_one_source():
    nav = TREE / "components" / "nav.html"
    assert nav.is_file(), "components/nav.html is the nav"
    assert "<nav" in nav.read_text(encoding="utf-8").lower()
    for frag in sorted((TREE / "pages").glob("*.html")):
        assert "<nav" not in frag.read_text(encoding="utf-8").lower(), f"{frag.name} must not have its own <nav>"


def test_index_hero_cards_cta(built):
    page = _page("index.html")
    heroes = page.with_class(None, "hero")
    assert heroes, "index needs a .hero"
    assert any(t == "h1" and "hero" in a["_classes_up"].split() for t, a in page.elements), "the hero holds an <h1>"
    assert len(page.with_class(None, "card")) >= 3, "index needs three .card elements"
    cta = [a for a in page.anchors if not a["in_nav"] and _norm(a["href"]) == "pricing.html"
           and ("cta" in a["class"].split() or "cta" in a["up"].split())]
    assert cta, "index needs a call to action (.cta, outside the nav) linking pricing.html"


def test_features_six(built):
    page = _page("features.html")
    feats = [a for t, a in page.elements if t == "article" and "feature" in a.get("class", "").split()]
    assert len(feats) >= 6, f"features needs six article.feature, found {len(feats)}"
    h3 = [a for t, a in page.elements if t == "h3" and "article" in a["_ancestors"]
          and "feature" in a["_classes_up"].split()]
    p = [a for t, a in page.elements if t == "p" and "article" in a["_ancestors"] and "feature" in a["_classes_up"].split()]
    assert len(h3) >= 6 and len(p) >= 6, "each feature has an <h3> and a <p>"


def test_pricing_table(built):
    page = _page("pricing.html")
    tables = [a for t, a in page.elements if t == "table" and "pricing-table" in a.get("class", "").split()]
    assert tables, "pricing needs table.pricing-table"
    assert any(t == "th" for t, _ in page.elements), "the table has a header row"
    rows = [a for t, a in page.elements if t == "tr"]
    assert len(rows) >= 4, "a header row and at least three feature rows"
    text = " ".join(" ".join(page.table_text).split())
    for word in ("Free", "Pro", "Team"):
        assert word in text
    assert re.search(r"\$\s*0\b", text) and re.search(r"\$\s*8\b", text) and re.search(r"\$\s*15\b", text)
    assert re.search(r"/\s*user", text, re.I) and re.search(r"/\s*mo", text, re.I)


def test_about_story_and_team(built):
    page = _page("about.html")
    assert len(" ".join(page.text).split()) >= 40, "about needs a story of at least 40 words"
    assert page.with_class(None, "team"), "about needs a .team section"
    figs = [a for t, a in page.elements if t == "figure" and "team" in a["_classes_up"].split()]
    assert len(figs) >= 3, "the team section has three <figure>s"
    names = [c for c in page.figcaptions if c]
    assert len(names) >= 3, "each person has a <figcaption> with a name"


def test_contact_form(built):
    page = _page("contact.html")
    assert [a for t, a in page.elements if t == "form" and "contact-form" in a.get("class", "").split()], \
        "contact needs form.contact-form"
    inputs = [a for t, a in page.elements if t == "input" and "form" in a["_ancestors"]]
    assert any(a.get("type", "").lower() == "email" for a in inputs), "an email field"
    assert any("name" in (a.get("name", "") + a.get("id", "")).lower() for a in inputs), "a name field"
    assert [a for t, a in page.elements if t == "textarea"], "a message <textarea>"
    assert len([1 for t, _ in page.elements if t == "label"]) >= 3, "the fields are labelled"
    submit = [a for t, a in page.elements if (t == "button" and a.get("type", "submit").lower() == "submit")
              or (t == "input" and a.get("type", "").lower() == "submit")]
    assert submit, "a submit button"
    assert any(a["href"].strip() == "mailto:hello@acme.example" for a in page.anchors), "a mailto link"


def test_styles_cover_components():
    css = (TREE / "styles.css").read_text(encoding="utf-8").lower()
    for sel in (".site-nav", ".hero", ".card", ".cta", ".feature", ".pricing-table", ".team", ".contact-form"):
        assert re.search(re.escape(sel) + r"(?![\w-])", css), f"styles.css must style {sel}"
    assert re.search(r"\[aria-current(=|\])", css), "styles.css must style the current nav link"


def test_styles_responsive_under_700px():
    css = (TREE / "styles.css").read_text(encoding="utf-8").lower()
    assert re.search(r"@media[^{]*max-width\s*:\s*700px", css), "styles.css needs @media (max-width: 700px)"


def test_one_stylesheet():
    css = sorted(p.relative_to(TREE).as_posix() for p in TREE.rglob("*.css") if "site" not in p.relative_to(TREE).parts)
    assert css == ["styles.css"], f"styles.css is the only stylesheet: {css}"


def test_local_links_resolve(built):
    for name in ORDER:
        for a in _page(name).anchors:
            href = a["href"].strip()
            if not href or href.startswith(("http:", "https:", "mailto:", "#", "tel:")):
                continue
            assert (SITE / _norm(href)).is_file(), f"{name} links missing {href}"
