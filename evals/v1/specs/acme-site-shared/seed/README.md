# Acme Notes site

The static marketing site for Acme Notes. Plain HTML and CSS, built by one small script with no dependencies.

    python build.py        # writes site/<page>.html and site/styles.css

- `pages/<name>.html` is a page fragment: its first line is `<!-- title: Page | Acme Notes -->` and the rest
  is the body content that goes inside `<main>`.
- `components/nav.html` is the site nav. `build.py` puts it at the top of every page.
- `styles.css` is the one stylesheet. `build.py` copies it into `site/`.
- `site/` is build output and is not committed.

Run the tests with `python -m pytest -q`.
