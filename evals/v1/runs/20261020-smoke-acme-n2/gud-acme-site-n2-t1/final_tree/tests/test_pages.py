import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["index.html", "pricing.html", "about.html"]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def nav(name):
    m = re.search(r"<nav.*?</nav>", read(name), re.S | re.I)
    assert m, name
    return m.group(0)


def test_pages_basics():
    for p in PAGES:
        html = read(p)
        assert html.lstrip().lower().startswith("<!doctype html>")
        assert "<title>" in html.lower()
        assert "styles.css" in html
        for target in PAGES:
            assert target in nav(p)


def test_nav_shared():
    navs = {re.sub(r"\s+", " ", nav(p)) for p in PAGES}
    assert len(navs) == 1


def test_index_links_pricing():
    assert 'href="pricing.html"' in read("index.html").split("</nav>")[1]


def test_pricing_plans():
    html = read("pricing.html")
    assert "<table" in html
    for s in ["Free", "Pro", "Team", "$8/mo", "$15/user/mo"]:
        assert s in html


def test_about_mailto():
    assert "mailto:hello@acme.example" in read("about.html")


def test_css_responsive():
    css = read("styles.css")
    assert "700px" in css and "@media" in css
