import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGES = ["index.html", "pricing.html", "about.html"]


@pytest.mark.parametrize("page", PAGES)
def test_page_basics(page):
    html = (ROOT / page).read_text().lower()
    assert html.lstrip().startswith("<!doctype html>")
    assert "<title>" in html
    assert 'href="styles.css"' in html
    nav = html[html.index("<nav"):html.index("</nav>")]
    for p in PAGES:
        assert f'href="{p}"' in nav


def test_index_cta_and_cards():
    html = (ROOT / "index.html").read_text()
    assert html.count('class="card"') == 3
    assert 'href="pricing.html"' in html


def test_pricing_plans():
    html = (ROOT / "pricing.html").read_text()
    assert "<table" in html
    for s in ["Free", "Pro", "$8/mo", "Team", "$15/user/mo"]:
        assert s in html


def test_about_contact():
    assert 'href="mailto:hello@acme.example"' in (ROOT / "about.html").read_text()


def test_css_responsive():
    assert "max-width: 700px" in (ROOT / "styles.css").read_text()
