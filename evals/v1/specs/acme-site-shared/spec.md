# Build the five-page Acme Notes website

This repo builds the static marketing site for Acme Notes, a note-taking app. `python build.py` renders every
page fragment in `pages/` through one shared template into `site/` (see README.md and build.py). Build the
full site, with these five pages:

- `pages/index.html` (Home): a hero (`class="hero"`) with an `<h1>`, three feature cards (`class="card"`), and a
  call to action (`class="cta"`, outside the nav) linking to pricing.html.
- `pages/features.html` (Features): at least six features, each an `<article class="feature">` with an `<h3>`
  and a `<p>`.
- `pages/pricing.html` (Pricing): a real `<table class="pricing-table">` comparing the Free ($0), Pro ($8/mo)
  and Team ($15/user/mo) plans, with a header row and at least three feature rows.
- `pages/about.html` (About): a short company story (at least 40 words) and a team section (`class="team"`)
  with at least three people, each a `<figure>` whose `<figcaption>` holds the person's name.
- `pages/contact.html` (Contact): a `<form class="contact-form">` with labelled name, email (`type="email"`)
  and message (`<textarea>`) fields and a submit button, and a `mailto:hello@acme.example` link.

The site has exactly one nav and one stylesheet, shared by every page:

- `components/nav.html` is the only nav (no page fragment has its own `<nav>`). It links all five pages in
  this order, with these link texts: Home (index.html), Features (features.html), Pricing (pricing.html),
  About (about.html), Contact (contact.html). In each built page, the nav link to that page carries
  `aria-current="page"` and no other nav link does.
- `styles.css` is the only stylesheet (no other `.css` files and no `<style>` blocks). It styles `.site-nav`,
  `.hero`, `.card`, `.cta`, `.feature`, `.pricing-table`, `.team`, `.contact-form` and the current nav link
  (`[aria-current="page"]`), and has a responsive layout under `@media (max-width: 700px)`.
- Every built page's `<title>` is `<Page name> | Acme Notes`, for example `Pricing | Acme Notes`, and every
  local link resolves to a built page.

`site/` is build output: do not commit it. Keep `python -m pytest -q` passing, and add tests under tests/ that
build the site and check the pages.
