"""Build the Acme Notes site: pages/<name>.html fragments + components/nav.html -> site/<name>.html.

    python build.py [OUT_DIR]      (default: site/)
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="styles.css">
</head>
<body>
{nav}
<main>
{body}
</main>
</body>
</html>
"""


def parse_page(text: str) -> tuple[str, str]:
    """(title, body) of a page fragment whose first line is `<!-- title: ... -->`."""
    first, _, rest = text.partition("\n")
    first = first.strip()
    if not (first.startswith("<!-- title:") and first.endswith("-->")):
        raise ValueError("a page fragment must start with <!-- title: ... -->")
    return first[len("<!-- title:"):-len("-->")].strip(), rest.strip()


def render(page: Path, nav: str) -> str:
    title, body = parse_page(page.read_text(encoding="utf-8"))
    return TEMPLATE.format(title=title, nav=nav, body=body)


def build(out: Path = ROOT / "site") -> Path:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    nav = (ROOT / "components" / "nav.html").read_text(encoding="utf-8").strip()
    for page in sorted((ROOT / "pages").glob("*.html")):
        (out / page.name).write_text(render(page, nav), encoding="utf-8")
    shutil.copy(ROOT / "styles.css", out / "styles.css")
    return out


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "site")
