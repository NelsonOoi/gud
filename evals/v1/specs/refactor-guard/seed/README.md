# notes

A small notes app. Each note is one Markdown file, `<slug>.md`, in the notes directory.

    python cli.py new Shopping list --body "milk, eggs"   # prints the slug: shopping-list
    python cli.py list
    python cli.py show shopping-list
    python cli.py delete shopping-list
    python cli.py gc                                      # tidy the notes directory

The notes directory is `--dir DIR`, else `$NOTES_DIR`, else `./notes-data`.

Code: `notes/slug.py` (titles to slugs), `notes/store.py` (reading and writing notes),
`notes/cleanup.py` (`gc`), `cli.py` (the command line). Run the tests with `python -m pytest -q`.
