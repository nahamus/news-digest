# News Digest

A personal news digest built from public RSS feeds by GitHub Actions twice a day
(~7 AM and ~5 PM Eastern) and published to GitHub Pages:
https://nahamus.github.io/news-digest/

## Change what you see

Edit `config.yaml`: categories, feeds, keywords, `mute` words, and the per-category and
per-source limits. Pushing a change to `main` rebuilds the site automatically.

## Build now

Actions tab → **Build digest** → **Run workflow**, or:

```
gh workflow run build.yml
```

## Build locally (optional)

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python build.py
```

Then open `site/index.html`. The log lists each feed and whether it failed.
Note that a local build also updates `data/seen.json`.

## Install on your phone

Open the Pages URL → Share (iOS) or ⋮ menu (Android) → **Add to Home Screen**.
The last digest stays readable offline.
