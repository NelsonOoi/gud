# Add unicode titles, tags, rename and search to the notes app

This repo is a small notes app (see README.md): `python cli.py [--dir DIR] COMMAND`, one Markdown file per
note. Add four features. Everything that works today must keep working.

1. **Unicode titles.** `notes.slug.slugify` keeps letters and digits from every script instead of dropping
   them: the title is NFC-normalised and lowercased with `str.lower()`, every run of characters that are not
   letters or digits (`str.isalnum()`) becomes one hyphen, and there is no leading or trailing hyphen.
   `slugify("Café au lait")` is `café-au-lait`, `slugify("Straße 5")` is `straße-5`, `slugify("東京 メモ")` is
   `東京-メモ`, and ASCII titles give the same slugs as before. A note whose title has a non-ASCII slug must work
   with every command.
2. **Tags.** `new TITLE... --tag TAG` (repeatable) stores tags in the note file as a second line
   `tags: home, errand` right after the title line (no such line when a note has no tags). Tags are
   lowercased, duplicates dropped, first occurrence kept. `tag SLUG TAG...` adds tags to a note and prints
   `Tagged SLUG: home, errand` (all its tags). `list --tag TAG` lists only notes with that tag. `show` prints
   the note file as stored.
3. **Rename.** `rename SLUG NEW TITLE...` changes the note's title and moves it to the slug of the new title
   (with the usual `-2`, `-3` suffix if that slug is taken by another note), keeps its tags and body, and
   prints the new slug.
4. **Search.** `search WORDS...` joins the words with spaces and prints `slug  Title` (two spaces) for every
   note whose title or body contains that text, ignoring case, sorted by slug; `No matches.` when none do.

An unknown slug prints `Error: no note 'x'` to stderr and exits 1 for every command that takes a slug.
Add tests under tests/ for every feature, and keep `python -m pytest -q` passing.
