# Build the Acme Notes marketing website

Turn this repo into a small marketing website for Acme Notes, a note-taking app.

- index.html: a landing page with a hero, three feature cards, and a call to action linking to pricing.html.
- pricing.html: three plans (Free, Pro $8/mo, Team $15/user/mo) in a comparison table.
- about.html: a short company story and a contact email link (mailto:hello@acme.example).
- styles.css: shared styles for the nav, hero, cards, and pricing table; a responsive layout under 700px.

Every page must keep <!doctype html>, a <title>, link styles.css, and share the same <nav> that links
index.html, pricing.html, and about.html. Keep `python -m pytest -q` passing, and add tests under tests/
that check the pages.
